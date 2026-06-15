#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
image_generator.py
ComfyUI API を使った YouTube 動画背景画像バッチ生成スクリプト
配置先: wikiproject\\scripts\\image_generator.py

【themes.csv status体系】
  0: 初期（theme_generator で作成）
  1: 原稿生成済み → genkou_generater.py で status=0→1 に更新
  2: 画像生成済み → image_generator.py で status=1→2 に更新（本スクリプト）
  3: 動画生成済み → movie_generater.py で status=1→3 に更新
  4: 投稿済み → youtube_uploader.py で status=3→4 に更新
  91: 原稿生成失敗スキップ ← genkou_generater.py でエラー時に status=91 を設定
  92: 画像生成失敗スキップ ← image_generator.py でリトライ超過時に status=92 を設定（本スクリプト）
  93: 動画生成失敗スキップ ← movie_generater.py でエラー時に status=93 を設定

【本スクリプトの処理フロー】
  status=1 のエントリに対して：
    1. ComfyUI で背景画像を生成（MAX_IMAGE_ATTEMPTS=3回までリトライ）
    2. 各試行後に image_checker で品質チェック
    3. 品質OK → status=2 に更新して成功
    4. 品質NG → 画像削除してリトライ
    5. リトライ超過 → status=92 に更新して失敗スキップ
"""

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import websocket
from datetime import datetime, timezone, timedelta
from pathlib import Path

# scripts/ 配下のモジュールをインポート
sys.path.insert(0, str(Path(__file__).parent))
from path_helper import (
    get_channel_data_dir,
    get_channel_config_dir,
    get_channel_output_dir,
    get_channel_logs_dir,
)
from image_checker import check_image

COMFYUI_URL = "http://127.0.0.1:8188"

# 1件あたりの最大生成リトライ回数（品質NGの場合に再生成）
MAX_IMAGE_ATTEMPTS = 3

JST = timezone(timedelta(hours=9))


# ── ログ設定 ──────────────────────────────────────────────
def setup_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"image_gen_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.log"

    logger = logging.getLogger("image_gen")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(row_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ファイルハンドラ
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # コンソールハンドラ
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


class RowAdapter(logging.LoggerAdapter):
    """ログに row_id を付与するアダプター。"""
    def process(self, msg, kwargs):
        kwargs.setdefault("extra", {})
        kwargs["extra"]["row_id"] = self.extra.get("row_id", "-")
        return msg, kwargs


# ── INI 読み込み ──────────────────────────────────────────
def load_ini(path: Path) -> dict[str, str]:
    """
    シンプルな [section] key=value 形式の INI を読み込む。
    返値: {"positive": "...", "negative": "..."}
    """
    result: dict[str, str] = {}
    current_section = None
    buf: list[str] = []

    def flush():
        if current_section:
            result[current_section] = "\n".join(buf).strip()

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                flush()
                current_section = stripped[1:-1].lower()
                buf = []
            elif current_section is not None:
                if not stripped.startswith("#"):
                    buf.append(line)
    flush()
    return result


# ── CSV 読み込み ──────────────────────────────────────────
def load_themes(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def update_status(path: Path, row_id: str, new_status: int) -> None:
    """themes.csv の id 列が一致する行の status を new_status に更新する。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
        fieldnames = rows[0].keys() if rows else []

    updated = False
    for row in rows:
        if row.get("id", "").strip() == str(row_id).strip():
            row["status"] = str(new_status)
            updated = True
            break

    if not updated:
        return

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ── ワークフロー構築 ──────────────────────────────────────
def build_workflow(
    positive: str,
    negative: str,
    seed: int,
    output_filename: str,
) -> dict:
    """
    ComfyUI 標準 txt2img ワークフロー（API形式）を組み立てる。

    ノード構成:
      4: CheckpointLoaderSimple
      6: CLIPTextEncode (positive)
      7: CLIPTextEncode (negative)
      3: KSampler
      5: EmptyLatentImage
      8: VAEDecode
      9: SaveImage
    """
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": "animemix_v80.safetensors",
            },
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width":   1280,
                "height":  2276,
                "batch_size": 1,
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": positive,
                "clip": ["4", 1],
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative,
                "clip": ["4", 1],
            },
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed":          seed,
                "steps":         20,
                "cfg":           7.0,
                "sampler_name":  "euler",
                "scheduler":     "normal",
                "denoise":       1.0,
                "model":         ["4", 0],
                "positive":      ["6", 0],
                "negative":      ["7", 0],
                "latent_image":  ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae":     ["4", 2],
            },
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": Path(output_filename).stem,
                "images":          ["8", 0],
            },
        },
    }


# ── ComfyUI API ───────────────────────────────────────────
def queue_prompt(workflow: dict, client_id: str) -> str:
    """
    ワークフローをキューに投入し、prompt_id を返す。
    """
    payload = json.dumps(
        {"prompt": workflow, "client_id": client_id}
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["prompt_id"]


def wait_for_completion(prompt_id: str, client_id: str, timeout: int = 300) -> dict:
    """
    WebSocket で進捗を監視し、実行完了後に /history から出力情報を返す。
    timeout: 秒（デフォルト300秒）
    """
    ws_url = f"ws://127.0.0.1:8188/ws?clientId={client_id}"
    ws = websocket.create_connection(ws_url, timeout=timeout)

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            raw = ws.recv()
            msg = json.loads(raw)
            mtype = msg.get("type", "")
            data  = msg.get("data", {})

            if mtype == "executing":
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    # 全ノード実行完了
                    break

            if mtype == "execution_error":
                if data.get("prompt_id") == prompt_id:
                    raise RuntimeError(f"ComfyUI execution_error: {data}")
        else:
            raise TimeoutError(f"prompt_id={prompt_id} が {timeout}s 以内に完了しませんでした")
    finally:
        ws.close()

    # 履歴から出力ファイル情報を取得
    url = f"{COMFYUI_URL}/history/{prompt_id}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        history = json.loads(resp.read())
    return history.get(prompt_id, {})


def download_image(filename: str, subfolder: str, dest: Path) -> None:
    """
    ComfyUI の /view エンドポイントから画像をダウンロードして dest に保存する。
    """
    params = urllib.parse.urlencode({
        "filename":  filename,
        "subfolder": subfolder,
        "type":      "output",
    })
    url = f"{COMFYUI_URL}/view?{params}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        dest.write_bytes(resp.read())


# ── 1件処理 ───────────────────────────────────────────────
def process_row(
    row: dict,
    ini: dict[str, str],
    output_dir: Path,
    log: RowAdapter,
) -> str:
    """
    1行分の処理を行い、"success" / "failed" / "skip" を返す。
    品質チェック失敗時は MAX_IMAGE_ATTEMPTS 回までリトライする。
    """
    row_id = row.get("id", "?").strip()
    status = row.get("status", "").strip()
    image  = row.get("image", "").strip()

    # status=1（原稿生成済み）のみ処理対象
    if status != "1":
        log.info(f"スキップ (status={status})")
        return "skip"

    if not image:
        log.warning("image 列が空のためスキップ")
        return "skip"

    # プロンプト組み立て
    positive_base = ini.get("positive", "")
    negative      = ini.get("negative", "")
    positive      = f"{image}, {positive_base}" if positive_base else image

    output_filename = f"{row_id}.png"
    dest_path       = output_dir / output_filename

    # 既に画像ファイルが存在する場合はスキップ
    if dest_path.exists():
        log.info(f"スキップ (画像ファイル既存: {dest_path})")
        return "skip"

    log.info(f"開始 | positive='{positive[:60]}...' | 最大リトライ={MAX_IMAGE_ATTEMPTS}")

    # ── MAX_IMAGE_ATTEMPTS 回までリトライ ──────────────────
    for attempt in range(1, MAX_IMAGE_ATTEMPTS + 1):
        log.info(f"--- 試行 {attempt}/{MAX_IMAGE_ATTEMPTS} ---")

        seed      = random.randint(0, 2**32 - 1)
        client_id = str(uuid.uuid4())
        workflow  = build_workflow(positive, negative, seed, output_filename)

        # キュー投入
        try:
            prompt_id = queue_prompt(workflow, client_id)
            log.info(f"キュー投入完了 prompt_id={prompt_id}")
        except Exception as e:
            log.error(f"キュー投入失敗: {e}")
            if attempt < MAX_IMAGE_ATTEMPTS:
                log.info(f"リトライ予定")
                continue
            else:
                log.error(f"全リトライ失敗 (キュー投入)")
                return "failed"

        # 完了待機
        try:
            history = wait_for_completion(prompt_id, client_id)
        except TimeoutError as e:
            log.error(f"タイムアウト: {e}")
            if attempt < MAX_IMAGE_ATTEMPTS:
                log.info(f"リトライ予定")
                continue
            else:
                log.error(f"全リトライ失敗 (タイムアウト)")
                return "failed"
        except Exception as e:
            log.error(f"実行エラー: {e}")
            if attempt < MAX_IMAGE_ATTEMPTS:
                log.info(f"リトライ予定")
                continue
            else:
                log.error(f"全リトライ失敗 (実行エラー)")
                return "failed"

        # 出力ファイル取得
        try:
            outputs = history.get("outputs", {})
            # SaveImage ノードの出力を探す
            image_info = None
            for node_output in outputs.values():
                images = node_output.get("images", [])
                if images:
                    image_info = images[0]
                    break

            if not image_info:
                log.error("ComfyUI 出力に画像情報が見つかりません")
                if attempt < MAX_IMAGE_ATTEMPTS:
                    log.info(f"リトライ予定")
                    continue
                else:
                    log.error(f"全リトライ失敗 (画像情報なし)")
                    return "failed"

            download_image(
                filename=image_info["filename"],
                subfolder=image_info.get("subfolder", ""),
                dest=dest_path,
            )
            log.info(f"ダウンロード完了 -> {dest_path}")

        except Exception as e:
            log.error(f"画像ダウンロード失敗: {e}")
            if attempt < MAX_IMAGE_ATTEMPTS:
                log.info(f"リトライ予定")
                continue
            else:
                log.error(f"全リトライ失敗 (ダウンロード)")
                return "failed"

        # ── 品質チェック ──────────────────────────────────────
        ok, reason = check_image(dest_path)
        if ok:
            log.info(f"品質チェック OK ✓")
            return "success"
        else:
            log.warning(f"品質チェック NG: {reason}")
            # ファイルを削除して次のリトライへ
            try:
                dest_path.unlink()
                log.info(f"ファイル削除: {dest_path}")
            except Exception as e:
                log.warning(f"ファイル削除失敗: {e}")

            if attempt < MAX_IMAGE_ATTEMPTS:
                log.info(f"リトライ予定")
                continue
            else:
                log.error(f"全リトライ失敗 (品質チェック)")
                return "failed"

    # ここには到達しないはずだが、念のため
    log.error("予期しないエラー: リトライループを抜けた")
    return "failed"


# ── メイン処理 ────────────────────────────────────────────
def main(channel: str = None):
    """
    Args:
        channel: チャンネル名（例: nazolabo）。None の場合はコマンドライン引数から取得
    """
    # コマンドライン引数を解析（channel が None の場合のみ）
    if channel is None:
        parser = argparse.ArgumentParser(description="背景画像自動生成")
        parser.add_argument("--channel", type=str, required=True, help="チャンネル名（例: nazolabo）")
        args = parser.parse_args()
        channel = args.channel

    # チャンネル別パスを取得
    DATA_DIR    = get_channel_data_dir(channel)
    CONFIG_DIR  = get_channel_config_dir(channel)
    OUTPUT_DIR  = get_channel_output_dir(channel) / "work"
    LOG_DIR     = get_channel_logs_dir(channel)

    THEMES_CSV  = DATA_DIR / "themes.csv"
    PROMPT_INI  = CONFIG_DIR / "image_prompt.ini"

    # ディレクトリ準備
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ロガー初期化
    base_logger = setup_logger(LOG_DIR)
    log = RowAdapter(base_logger, {"row_id": "-"})

    log.info("=== image_generator 開始 ===")

    # INI 読み込み
    if not PROMPT_INI.exists():
        log.error(f"INI ファイルが見つかりません: {PROMPT_INI}")
        sys.exit(1)
    ini = load_ini(PROMPT_INI)
    log.info(f"INI 読み込み完了: {PROMPT_INI}")

    # CSV 読み込み
    if not THEMES_CSV.exists():
        log.error(f"CSV ファイルが見つかりません: {THEMES_CSV}")
        sys.exit(1)
    rows = load_themes(THEMES_CSV)
    log.info(f"CSV 読み込み完了: {len(rows)} 件")

    # ComfyUI 疎通確認
    try:
        with urllib.request.urlopen(f"{COMFYUI_URL}/system_stats", timeout=5) as resp:
            stats = json.loads(resp.read())
        log.info(f"ComfyUI 接続OK: {COMFYUI_URL}")
    except Exception as e:
        log.error(f"ComfyUI に接続できません ({COMFYUI_URL}): {e}")
        sys.exit(1)

    # status=1 の件数をログ出力
    pending = [r for r in rows if r.get("status", "").strip() == "1"]
    log.info(f"処理対象 (status=1): {len(pending)} 件 / 全{len(rows)} 件")

    # バッチ処理
    total = len(rows)
    skipped = success = failed = 0

    for i, row in enumerate(rows, start=1):
        row_id  = row.get("id", "?").strip()
        row_log = RowAdapter(base_logger, {"row_id": row_id})
        row_log.info(f"処理開始 ({i}/{total})")

        result = process_row(row, ini, OUTPUT_DIR, row_log)

        if result == "skip":
            skipped += 1
        elif result == "success":
            success += 1
            # status=2（背景生成済み）に更新
            try:
                update_status(THEMES_CSV, row_id, 2)
                row_log.info(f"themes.csv status -> 2")
            except Exception as e:
                row_log.warning(f"status更新失敗: {e}")
        else:
            failed += 1
            # status=92（画像生成失敗スキップ）に更新
            try:
                update_status(THEMES_CSV, row_id, 92)
                row_log.info(f"themes.csv status -> 92")
            except Exception as e:
                row_log.warning(f"status更新失敗: {e}")

    # サマリー
    log.info(
        f"=== 完了 | 合計={total} 成功={success} 失敗={failed} スキップ={skipped} ==="
    )


if __name__ == "__main__":
    main()
