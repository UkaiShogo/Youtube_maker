#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manager.py
YouTube Shorts 自動生成・投稿 パイプラインマネージャー
配置先: wikiproject\\scripts\\manager.py

【実行タイミング】
  OSのタスクスケジューラで 平日 03:00 JST に起動する。
  run_pipeline.bat から python manager.py を呼び出す。

【処理フロー】
  在庫チェック
    → theme_generator    status=0 が theme_min  未満なら実行（目標 theme_target 件）
    → genkou_generater   status=1 が genkou_min 未満なら実行
    → image_generator    status=1 があれば実行（背景画像生成）
    → movie_generater    status=2 が movie_min  未満なら実行
    → youtube_uploader   status=3 があれば実行（最大 max_upload 本）
  ※ 閾値はすべて config/manager_config.ini で設定

【失敗時の挙動】
  各ステップの失敗をログに記録したうえで後続ステップも続行する。

【在庫定義（themes.csv の status 列）】
  status=0 : テーマ登録済み         → テーマ在庫
  status=1 : 原稿生成済み           → 原稿在庫
  status=2 : 背景生成済み           → 背景在庫
  status=3 : 動画生成済み           → 動画在庫
  status=4 : 動画アップロード済み
  status=5 : 失敗
"""

import argparse
import csv
import configparser
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

# scripts/ 配下のモジュールをインポート
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from path_helper import (
    get_base_dir,
    get_channel_dir,
    get_channel_data_dir,
    get_channel_config_dir,
    get_channel_logs_dir,
)
from log_writer import write_manager_log, write_process_log

# ===== 外部ツールパス =====
VOICEVOX_EXE   = Path(r"あなたのVOICEVOXのexeファイルを絶対パスで指定してください")
COMFYUI_BAT    = Path(__file__).resolve().parent.parent.parent / "gen_image" / "ComfyUI" / "start_comfyui.bat"
VOICEVOX_PORT  = 50021
COMFYUI_PORT   = 8188
COMFYUI_WAIT_SEC = 60   # ComfyUI 起動待機時間（秒）

JST = timezone(timedelta(hours=9))


# ============================================================
#  外部ツール起動チェック
# ============================================================
def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """指定ポートに接続できるか確認する。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_voicevox():
    """
    VOICEVOX が起動していなければ起動する。
    ポート 50021 が応答するまで待機。タイムアウト時は RuntimeError を送出。
    """
    if _is_port_open("127.0.0.1", VOICEVOX_PORT):
        print(f"  [VOICEVOX] 起動済み (port {VOICEVOX_PORT})")
        return

    print(f"  [VOICEVOX] 未起動 → 起動します: {VOICEVOX_EXE}")
    if not VOICEVOX_EXE.exists():
        raise RuntimeError(f"[VOICEVOX] 実行ファイルが見つかりません: {VOICEVOX_EXE}")

    subprocess.Popen(
        [str(VOICEVOX_EXE)],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )

    print("  [VOICEVOX] 起動待機中", end="", flush=True)
    for _ in range(30):
        time.sleep(1)
        print(".", end="", flush=True)
        if _is_port_open("127.0.0.1", VOICEVOX_PORT):
            print(" OK")
            return
    print()
    raise RuntimeError(f"[VOICEVOX] 30秒待機しましたが port {VOICEVOX_PORT} が応答しません")


def ensure_comfyui():
    """
    ComfyUI が起動していなければ起動する。
    ポート 8188 が応答するまで COMFYUI_WAIT_SEC 秒待機。タイムアウト時は RuntimeError を送出。
    """
    if _is_port_open("127.0.0.1", COMFYUI_PORT):
        print(f"  [ComfyUI] 起動済み (port {COMFYUI_PORT})")
        return

    print(f"  [ComfyUI] 未起動 → 起動します: {COMFYUI_BAT}")
    if not COMFYUI_BAT.exists():
        raise RuntimeError(f"[ComfyUI] batファイルが見つかりません: {COMFYUI_BAT}")

    subprocess.Popen(
        ["cmd.exe", "/c", str(COMFYUI_BAT)],
        cwd=str(COMFYUI_BAT.parent),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )

    print(f"  [ComfyUI] 起動待機中（最大 {COMFYUI_WAIT_SEC} 秒）", end="", flush=True)
    for _ in range(COMFYUI_WAIT_SEC):
        time.sleep(1)
        print(".", end="", flush=True)
        if _is_port_open("127.0.0.1", COMFYUI_PORT):
            print(" OK")
            return
    print()
    raise RuntimeError(f"[ComfyUI] {COMFYUI_WAIT_SEC}秒待機しましたが port {COMFYUI_PORT} が応答しません")


# ============================================================
#  コンフィグ読み込み
# ============================================================
def load_config(config_path: Path) -> configparser.ConfigParser:
    if not config_path.exists():
        raise FileNotFoundError(
            f"設定ファイルが見つかりません: {config_path}\n"
            f"config/manager_config.ini を作成してください。"
        )
    cfg = configparser.ConfigParser()
    cfg.read(str(config_path), encoding="utf-8")
    return cfg


# ============================================================
#  ユーティリティ
# ============================================================
def make_run_id() -> str:
    return datetime.now(JST).strftime("%Y%m%d_%H%M%S")


def count_status(csv_path: Path) -> dict[str, int]:
    """
    themes.csv を読み込み、status ごとの件数を返す。
    例) {"0": 15, "1": 8, "2": 5, "3": 2, "4": 1}
    """
    counts: dict[str, int] = {}
    if not csv_path.exists():
        return counts
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            s = str(row.get("status", "")).strip()
            counts[s] = counts.get(s, 0) + 1
    return counts


def print_stock(counts: dict[str, int]):
    print(f"  テーマ在庫 (status=0) : {counts.get('0', 0):3d} 件")
    print(f"  原稿在庫   (status=1) : {counts.get('1', 0):3d} 件")
    print(f"  背景在庫   (status=2) : {counts.get('2', 0):3d} 件")
    print(f"  動画在庫   (status=3) : {counts.get('3', 0):3d} 件")
    print(f"  投稿済み   (status=4) : {counts.get('4', 0):3d} 件")
    print(f"  失敗       (status=5) : {counts.get('5', 0):3d} 件")


# ============================================================
#  各スクリプトの main() を import して呼び出す
# ============================================================
def run_theme_generator(channel: str, generate_count: int = None) -> None:
    """theme_generator.py の main() を呼び出す。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "theme_generator", SCRIPTS_DIR / "theme_generator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(channel=channel, generate_count=generate_count)


def run_genkou_generater(channel: str, max_count: int = None) -> None:
    """genkou_generater.py の main() を呼び出す。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "genkou_generater", SCRIPTS_DIR / "genkou_generater.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(channel=channel, max_count=max_count)


def run_image_generator(channel: str) -> None:
    """image_generator.py の main() を呼び出す。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "image_generator", SCRIPTS_DIR / "image_generator.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(channel=channel)


def run_movie_generater(channel: str, max_count: int = None) -> None:
    """movie_generater.py の main() を呼び出す。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "movie_generater", SCRIPTS_DIR / "movie_generater.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(channel=channel, max_count=max_count)


def run_youtube_uploader(channel: str, max_count: int = None) -> None:
    """youtube_uploader.py の main() を呼び出す。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "youtube_uploader", SCRIPTS_DIR / "youtube_uploader.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(channel=channel, max_count=max_count)


# ============================================================
#  ステップ実行ラッパー
#  失敗してもログに記録して呼び出し元に False を返す（後続続行）
# ============================================================
def run_step(
    name: str,
    func,
    run_id: str,
) -> tuple[str, float]:
    """
    func() を実行し (status, duration_sec) を返す。
    status: "success" / "failed"
    """
    print(f"\n{'─'*50}")
    print(f"  [{name}] 開始")
    print(f"{'─'*50}")
    t_start = time.monotonic()
    try:
        func()
        duration = time.monotonic() - t_start
        print(f"\n  [{name}] ✔ 完了  ({duration:.1f}s)")
        write_process_log(
            LOG_DIR, video_id="", process=name,
            status="success", duration_sec=duration,
        )
        return "success", duration
    except SystemExit as e:
        # sys.exit(0) は正常終了扱い、それ以外は失敗
        duration = time.monotonic() - t_start
        if e.code == 0:
            print(f"\n  [{name}] ✔ 完了（正常終了）  ({duration:.1f}s)")
            write_process_log(
                LOG_DIR, video_id="", process=name,
                status="success", duration_sec=duration,
            )
            return "success", duration
        err = f"SystemExit({e.code})"
        print(f"\n  [{name}] ✖ 失敗: {err}")
        write_process_log(
            LOG_DIR, video_id="", process=name,
            status="failed", duration_sec=duration,
            error_message=err,
        )
        return "failed", duration
    except Exception as e:
        duration = time.monotonic() - t_start
        err = str(e)
        print(f"\n  [{name}] ✖ 失敗: {err}")
        traceback.print_exc()
        write_process_log(
            LOG_DIR, video_id="", process=name,
            status="failed", duration_sec=duration,
            error_message=err,
        )
        return "failed", duration


# ============================================================
#  メイン処理
# ============================================================
def main(channel: str = None):
    """
    Args:
        channel: チャンネル名（例: nazolabo）。None の場合はコマンドライン引数から取得
    """
    # コマンドライン引数を解析（channel が None の場合のみ）
    if channel is None:
        parser = argparse.ArgumentParser(description="YouTube Shorts パイプラインマネージャー")
        parser.add_argument("--channel", type=str, required=True, help="チャンネル名（例: nazolabo）")
        args = parser.parse_args()
        channel = args.channel

    # チャンネル別パスを取得
    DATA_DIR    = get_channel_data_dir(channel)
    LOG_DIR     = get_channel_logs_dir(channel)
    CONFIG_DIR  = get_channel_config_dir(channel)

    THEMES_CSV  = DATA_DIR / "themes.csv"
    CONFIG_PATH = CONFIG_DIR / "manager_config.ini"

    run_id    = make_run_id()
    run_start = time.monotonic()

    # ── 外部ツール起動確認 ────────────────────────────────────
    print("=" * 55)
    print("  外部ツール起動チェック")
    print("=" * 55)
    try:
        ensure_voicevox()
        ensure_comfyui()
    except RuntimeError as e:
        print(f"\n  ✖ 起動チェック失敗: {e}")
        write_process_log(
            LOG_DIR, video_id="", process="startup_check",
            status="failed", duration_sec=time.monotonic() - run_start,
            error_message=str(e),
        )
        write_manager_log(
            LOG_DIR,
            run_id       = run_id,
            status       = "failed",
            duration_sec = time.monotonic() - run_start,
            step_theme   = "skipped",
            step_genkou  = "skipped",
            step_movie   = "skipped",
            step_upload  = "skipped",
            error_message = str(e),
        )
        print("  パイプラインを中断しました。")
        return
    print()

    # ── コンフィグ読み込み ────────────────────────────────────
    cfg = load_config(CONFIG_PATH)

    # パス上書き（config に記載があれば）
    themes_csv = Path(cfg.get("paths", "themes_csv", fallback=str(THEMES_CSV)))
    log_dir    = Path(cfg.get("paths", "log_dir",    fallback=str(LOG_DIR)))

    # 在庫閾値・目標値
    theme_min    = cfg.getint("stock",  "theme_min",    fallback=30)
    theme_target = cfg.getint("stock",  "theme_target", fallback=80)
    genkou_min   = cfg.getint("stock",  "genkou_min",   fallback=20)
    image_min    = cfg.getint("stock",  "image_min",    fallback=10)
    movie_min    = cfg.getint("stock",  "movie_min",    fallback=16)
    max_upload   = cfg.getint("upload", "max_upload",   fallback=2)

    # 在庫目標数・投稿上限数（[limit] セクション）
    # None = 未設定 → 各スクリプトのデフォルト動作
    def _get_limit(key: str):
        try:
            return cfg.getint("limit", key)
        except Exception:
            return None

    lim_theme_target  = _get_limit("theme_target_stock")   # テーマ在庫の目標数
    lim_genkou_target = _get_limit("genkou_target_stock")   # 原稿在庫の目標数
    lim_image_target  = _get_limit("image_target_stock")    # 背景在庫の目標数
    lim_movie_target  = _get_limit("movie_target_stock")    # 動画在庫の目標数
    lim_upload_count  = _get_limit("upload_max_count")      # 投稿上限（目標方式ではなく上限値）
    effective_upload_max = lim_upload_count if lim_upload_count is not None else max_upload

    print("=" * 55)
    print(f"  YouTube Shorts パイプライン マネージャー")
    print(f"  run_id: {run_id}")
    print("=" * 55)
    def _fmt_lim(v): return f"{v} 件" if v is not None else "制限なし"
    print(f"\n【閾値・上限設定】")
    print(f"  テーマ  : 最低 {theme_min} 件 / 補充目標 {theme_target} 件 / 在庫目標 {_fmt_lim(lim_theme_target)}")
    print(f"  原稿    : 最低 {genkou_min} 件 / 在庫目標 {_fmt_lim(lim_genkou_target)}")
    print(f"  背景    : 最低 {image_min} 件 / 在庫目標 {_fmt_lim(lim_image_target)}")
    print(f"  動画    : 最低 {movie_min} 件 / 在庫目標 {_fmt_lim(lim_movie_target)}")
    print(f"  投稿    : 最大 {effective_upload_max} 本/回")

    # ── 在庫チェック ─────────────────────────────────────────
    counts = count_status(themes_csv)
    stock_theme  = counts.get("0", 0)
    stock_genkou = counts.get("1", 0)
    stock_image  = counts.get("2", 0)
    stock_movie  = counts.get("3", 0)

    print("\n【現在の在庫】")
    print_stock(counts)

    # ── 各ステップの実行要否を決定 ───────────────────────────
    need_theme  = stock_theme  < theme_min
    need_genkou = stock_genkou < genkou_min
    need_image  = stock_genkou > 0   # status=1 が1件でもあれば背景生成
    need_movie  = stock_movie < movie_min
    # need_upload は movie_generater 実行後に再評価するためここでは表示のみ

    print("\n【実行計画】")
    print(f"  theme_generator  : {'実行  (在庫 {0} < 閾値 {1})'.format(stock_theme,  theme_min)  if need_theme  else 'スキップ (在庫 {0} >= 閾値 {1})'.format(stock_theme,  theme_min)}")
    print(f"  genkou_generater : {'実行  (在庫 {0} < 閾値 {1})'.format(stock_genkou, genkou_min) if need_genkou else 'スキップ (在庫 {0} >= 閾値 {1})'.format(stock_genkou, genkou_min)}")
    print(f"  image_generator  : {'実行  (status=1 が {0} 件)'.format(stock_genkou)              if need_image  else 'スキップ (status=1 の件数が 0)'}")
    print(f"  movie_generater  : {'実行  (在庫 {0} < 閾値 {1})'.format(stock_movie,  movie_min)  if need_movie  else 'スキップ (在庫 {0} >= 閾値 {1})'.format(stock_movie,  movie_min)}")
    print(f"  youtube_uploader : movie_generater 実行後に判定")

    # ── ステップ実行 ──────────────────────────────────────────
    results: dict[str, str] = {
        "theme":  "skipped",
        "genkou": "skipped",
        "image":  "skipped",
        "movie":  "skipped",
        "upload": "skipped",
    }

    # --- 1. theme_generator ---
    if need_theme:
        import os
        os.environ["THEME_GENERATE_TARGET"] = str(theme_target)
        # 目標在庫数が設定されている場合は (目標 - 現在) 件だけ生成
        theme_gen_count = (
            max(0, lim_theme_target - stock_theme)
            if lim_theme_target is not None else None
        )
        if theme_gen_count == 0:
            print("  [theme_generator] 在庫が目標数に達しているためスキップ")
            results["theme"] = "skipped"
        else:
            print(f"  [theme_generator] 生成件数: {theme_gen_count if theme_gen_count else '全件'}"
                  + (f" (目標 {lim_theme_target} - 現在 {stock_theme})" if lim_theme_target else ""))
            status, _ = run_step(
                "theme_generator",
                lambda: run_theme_generator(channel, generate_count=theme_gen_count),
                run_id,
            )
            results["theme"] = status
        counts      = count_status(themes_csv)
        stock_theme = counts.get("0", 0)
        print(f"  [theme_generator] 実行後 status=0: {stock_theme} 件")

    # --- 2. genkou_generater ---
    if need_genkou:
        # 目標在庫数が設定されている場合は (目標 - 現在) 件だけ処理
        genkou_gen_count = (
            max(0, lim_genkou_target - stock_genkou)
            if lim_genkou_target is not None else None
        )
        if genkou_gen_count == 0:
            print("  [genkou_generater] 在庫が目標数に達しているためスキップ")
            results["genkou"] = "skipped"
        else:
            print(f"  [genkou_generater] 処理件数: {genkou_gen_count if genkou_gen_count else '全件'}"
                  + (f" (目標 {lim_genkou_target} - 現在 {stock_genkou})" if lim_genkou_target else ""))
            status, _ = run_step(
                "genkou_generater",
                lambda: run_genkou_generater(channel, max_count=genkou_gen_count),
                run_id,
            )
            results["genkou"] = status
        counts       = count_status(themes_csv)
        stock_genkou = counts.get("1", 0)
        print(f"  [genkou_generater] 実行後 status=1: {stock_genkou} 件")

    # --- 3. image_generator ---
    # genkou実行後に再チェック（genkou実行でstatus=1が増えた可能性）
    counts      = count_status(themes_csv)
    stock_genkou = counts.get("1", 0)
    need_image  = stock_genkou > 0
    if need_image:
        status, _ = run_step(
            "image_generator",
            lambda: run_image_generator(channel),
            run_id,
        )
        results["image"] = status
        counts       = count_status(themes_csv)
        stock_image  = counts.get("2", 0)
        print(f"  [image_generator] 実行後 status=2: {stock_image} 件")

    # --- 4. movie_generater ---
    counts      = count_status(themes_csv)
    stock_image = counts.get("2", 0)
    stock_movie = counts.get("3", 0)
    need_movie  = stock_movie < movie_min
    if need_movie:
        # 目標在庫数が設定されている場合は (目標 - 現在) 本だけ生成
        movie_gen_count = (
            max(0, lim_movie_target - stock_movie)   # stock_movieと比較（stock_imageではない）
            if lim_movie_target is not None else None
        )
        if movie_gen_count == 0:
            print("  [movie_generater] 在庫が目標数に達しているためスキップ")
            results["movie"] = "skipped"
        else:
            print(f"  [movie_generater] 生成本数: {movie_gen_count if movie_gen_count else '全件'}"
                  + (f" (目標 {lim_movie_target} - 現在 {stock_movie})" if lim_movie_target else ""))
            status, _ = run_step(
                "movie_generater",
                lambda: run_movie_generater(channel, max_count=movie_gen_count),
                run_id,
            )
            results["movie"] = status
        counts      = count_status(themes_csv)
        stock_movie = counts.get("3", 0)
        print(f"  [movie_generater] 実行後 status=3: {stock_movie} 件")

    # --- 5. youtube_uploader ---
    # movie_generater 実行後に status=3 を再チェック
    counts      = count_status(themes_csv)
    stock_movie = counts.get("3", 0)
    need_upload = stock_movie > 0
    print(f"  [youtube_uploader] 判定: status=3 が {stock_movie} 件 → {'実行' if need_upload else 'スキップ'}")
    if need_upload:
        status, _ = run_step(
            "youtube_uploader",
            lambda: run_youtube_uploader(channel, max_count=effective_upload_max),
            run_id,
        )
        results["upload"] = status

    # ── サマリー ─────────────────────────────────────────────
    run_duration = time.monotonic() - run_start
    any_failed   = any(v == "failed" for v in results.values())
    run_status   = "failed" if any_failed else (
                   "skipped" if all(v == "skipped" for v in results.values()) else "success"
    )

    print(f"\n{'='*55}")
    print(f"  パイプライン完了  [{run_status.upper()}]  ({run_duration:.1f}s)")
    print(f"{'='*55}")
    print(f"  theme_generator  : {results['theme']}")
    print(f"  genkou_generater : {results['genkou']}")
    print(f"  image_generator  : {results['image']}")
    print(f"  movie_generater  : {results['movie']}")
    print(f"  youtube_uploader : {results['upload']}")

    write_manager_log(
        log_dir,
        run_id              = run_id,
        status              = run_status,
        duration_sec        = run_duration,
        step_theme          = results["theme"],
        step_genkou         = results["genkou"],
        step_movie          = results["movie"],
        step_upload         = results["upload"],
        stock_theme_before  = stock_theme,
        stock_genkou_before = stock_genkou,
        stock_movie_before  = stock_image,
        error_message       = (
            "一部ステップが失敗しました: " +
            ", ".join(k for k, v in results.items() if v == "failed")
        ) if any_failed else "",
    )


if __name__ == "__main__":
    main()
