#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
theme_generator.py
themes.csv 自動テーマ生成スクリプト

【themes.csv status体系】
  0: 初期（本スクリプトで新規作成）← パイプラインの起点
  1: 原稿生成済み → genkou_generater.py で status=0→1 に更新
  2: 画像生成済み → image_generator.py で status=1→2 に更新
  3: 動画生成済み → movie_generater.py で status=1→3 に更新
  4: 投稿済み → youtube_uploader.py で status=3→4 に更新
  91: 原稿生成失敗スキップ ← genkou_generater.py でエラー時に status=91 を設定
  92: 画像生成失敗スキップ ← image_generator.py でリトライ超過時に status=92 を設定
  93: 動画生成失敗スキップ ← movie_generater.py でエラー時に status=93 を設定

【本スクリプトの処理フロー】
  Claude API でテーマを自動生成 → themes.csv に status=0 で新規レコード追加
  その後、パイプライン内の各スクリプトが status を更新していく
"""

import argparse
import csv
import os
import sys
import re
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

# scripts/ 配下のモジュールをインポート
sys.path.insert(0, str(Path(__file__).parent))
from path_helper import (
    get_channel_data_dir,
    get_channel_config_dir,
    get_channel_prompts_dir,
    get_channel_logs_dir,
)
from log_writer import write_theme_gen_log, write_process_log

JST = timezone(timedelta(hours=9))


# ── 実行ID生成 ────────────────────────────────────────────
def make_run_id() -> str:
    """YYYYMMDD_HHMMSS 形式の実行識別子を返す。"""
    return datetime.now(JST).strftime("%Y%m%d_%H%M%S")


# ── 設定読み込み ──────────────────────────────────────────
def load_config(path: Path) -> dict:
    config = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
    return config


# ── themes.csv 読み込み ───────────────────────────────────
def load_themes(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ── status=0 カウント ────────────────────────────────────
def count_unprocessed(themes: list[dict]) -> int:
    return sum(1 for t in themes if str(t.get("status", "")).strip() == "0")


# ── 最大 id 取得 ─────────────────────────────────────────
def get_max_id(themes: list[dict]) -> int:
    ids = []
    for t in themes:
        try:
            ids.append(int(t["id"]))
        except (KeyError, ValueError):
            pass
    return max(ids) if ids else 0


# ── 既存 name 一覧 ───────────────────────────────────────
def get_existing_names(themes: list[dict]) -> set[str]:
    return {t["name"].strip() for t in themes if "name" in t}


# ── プロンプト読み込み ────────────────────────────────────
def load_prompt(path: Path, **kwargs) -> str:
    text = path.read_text(encoding="utf-8")
    for key, val in kwargs.items():
        text = text.replace("{" + key + "}", str(val))
    return text


# ── Claude API 呼び出し ───────────────────────────────────
def call_claude(system_prompt: str, user_prompt: str, model: str) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic パッケージがインストールされていません。"
            "pip install anthropic を実行してください。"
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("環境変数 ANTHROPIC_API_KEY が設定されていません。")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    )
    return response.content[0].text.strip()


# ── Claude 出力パース ─────────────────────────────────────
def parse_generated_csv(raw: str) -> list[dict]:
    """
    Claude が返す CSV 形式をパースして
    {"name": "...", "themes": "...", "hook_type": "...", "image": "..."} のリストを返す。

    形式: name,themes,hook_type,image
    例: なぜ火山は爆発するの？,自然,basic,volcano erupting; molten lava; smoke; red hot fire; ground shaking
    """
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # code blockをスキップ
        if line.startswith("```"):
            continue
        # ヘッダー行をスキップ（name,themes,hook_type,imageの場合）
        if line.lower().startswith("name,"):
            continue

        # CSV形式で分割（最大4列）
        parts = line.split(",", maxsplit=3)
        if len(parts) >= 2:  # 最低でも name と themes は必須
            try:
                name      = parts[0].strip()
                themes    = parts[1].strip()
                hook_type = parts[2].strip() if len(parts) > 2 else ""
                image     = parts[3].strip() if len(parts) > 3 else ""

                # name が空でない場合のみ追加
                if name:
                    results.append({
                        "name":      name,
                        "themes":    themes,
                        "hook_type": hook_type,
                        "image":     image,
                    })
            except Exception:
                # パース失敗はスキップ
                continue
    return results


# ── themes.csv 追記 ───────────────────────────────────────
def append_themes(path: Path, new_rows: list[dict]) -> None:
    """
    themes.csv に追記する。ファイルが存在しない or 空の場合はヘッダーも書く。
    新フォーマット: id, name, themes, image, channel, hook_type, status, created_at
    """
    fieldnames = ["id", "name", "themes", "image", "channel", "hook_type", "status", "created_at"]
    needs_header = not path.exists() or path.stat().st_size == 0
    with open(path, encoding="utf-8-sig", newline="", mode="a") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if needs_header:
            writer.writeheader()
        writer.writerows(new_rows)


# ── メイン処理 ────────────────────────────────────────────
def main(channel: str = None, generate_count: int = None):
    """
    Args:
        channel: チャンネル名（例: nazolabo）。None の場合はコマンドライン引数から取得
        generate_count: 生成するテーマ数。None の場合は theme_gen_config.txt の
                        THEME_GENERATE_COUNT を使用（単体起動時のデフォルト）。
                        manager.py から呼ぶ場合に上限値を渡す。
    """
    # コマンドライン引数を解析（channel が None の場合のみ）
    if channel is None:
        parser = argparse.ArgumentParser(description="YouTube Shorts テーマ自動生成")
        parser.add_argument("--channel", type=str, required=True, help="チャンネル名（例: nazolabo）")
        parser.add_argument("--generate-count", type=int, default=None, help="生成数（config より優先）")
        args = parser.parse_args()
        channel = args.channel
        if args.generate_count is not None:
            generate_count = args.generate_count

    run_id     = make_run_id()
    run_start  = time.monotonic()

    # チャンネル別パスを取得
    DATA_DIR    = get_channel_data_dir(channel)
    CONFIG_DIR  = get_channel_config_dir(channel)
    PROMPTS_DIR = get_channel_prompts_dir(channel)
    LOG_DIR     = get_channel_logs_dir(channel)

    THEMES_CSV    = DATA_DIR / "themes.csv"
    CONFIG_FILE   = CONFIG_DIR / "theme_gen_config.txt"
    SYSTEM_PROMPT = PROMPTS_DIR / "theme_system.txt"
    USER_PROMPT   = PROMPTS_DIR / "theme_user.txt"

    # --- 設定読み込み ---
    config    = load_config(CONFIG_FILE)
    threshold = int(config.get("THEME_THRESHOLD",      20))
    model     = config.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    # generate_count: 引数 > config の優先順位
    if generate_count is None:
        generate_count = int(config.get("THEME_GENERATE_COUNT", 30))
    else:
        print(f"[設定] generate_count={generate_count} (manager指定で上書き)")

    # --- themes.csv 読み込み ---
    themes       = load_themes(THEMES_CSV)
    stock_before = count_unprocessed(themes)

    print(f"theme stock : {stock_before}")

    # ── 閾値チェック ──────────────────────────────────────
    t_start = time.monotonic()
    if stock_before > threshold:
        t_dur = time.monotonic() - t_start
        print(f"above threshold ({threshold}) : no action needed")

        write_process_log(
            LOG_DIR, video_id="", process="threshold_check",
            status="skipped", title=f"stock={stock_before} > threshold={threshold}",
            duration_sec=t_dur,
        )
        write_theme_gen_log(
            LOG_DIR, run_id=run_id, status="skipped",
            duration_sec=time.monotonic() - run_start,
            api_status="skipped", stock_before=stock_before, stock_after=stock_before,
        )
        return

    t_dur = time.monotonic() - t_start
    print(f"below threshold : generating themes\n")
    write_process_log(
        LOG_DIR, video_id="", process="threshold_check",
        status="success", title=f"stock={stock_before} <= threshold={threshold}",
        duration_sec=t_dur,
    )

    # 最大 id・既存名取得
    max_id         = get_max_id(themes)
    existing_names = get_existing_names(themes)

    # ── プロンプト読み込み & API 呼び出し ─────────────────
    system_prompt = load_prompt(SYSTEM_PROMPT)
    user_prompt   = load_prompt(USER_PROMPT, generate_count=generate_count)

    api_status    = "failed"
    raw_output    = ""
    api_error     = ""
    generated     = []
    api_start     = time.monotonic()

    try:
        raw_output = call_claude(system_prompt, user_prompt, model)
        api_dur    = time.monotonic() - api_start
        api_status = "success"
        generated  = parse_generated_csv(raw_output)

        print(f"generated : {len(generated)}")
        write_process_log(
            LOG_DIR, video_id="", process="api_call",
            status="success",
            title=f"model={model} generated={len(generated)}",
            duration_sec=api_dur,
        )

    except Exception as e:
        api_dur    = time.monotonic() - api_start
        api_error  = str(e)
        print(f"[ERROR] API 呼び出し失敗: {api_error}")

        write_process_log(
            LOG_DIR, video_id="", process="api_call",
            status="failed", title=f"model={model}",
            duration_sec=api_dur, error_message=api_error,
        )
        write_theme_gen_log(
            LOG_DIR, run_id=run_id, status="failed",
            duration_sec=time.monotonic() - run_start,
            api_status="failed", stock_before=stock_before,
            error_message=api_error,
        )
        sys.exit(1)

    # ── 重複チェック & ID 付与 ───────────────────────────
    new_rows   = []
    duplicates = 0
    created_at = datetime.now(JST).isoformat()

    for item in generated:
        name = item["name"].strip()
        if name in existing_names:
            duplicates += 1
            continue
        max_id += 1
        new_rows.append({
            "id":        max_id,
            "name":      name,
            "themes":    item.get("themes", "").strip(),
            "image":     item.get("image", "").strip(),
            "channel":   "nazewhy",
            "hook_type": item.get("hook_type", "").strip(),
            "status":    0,
            "created_at": created_at,
        })
        existing_names.add(name)

    added = len(new_rows)
    print(f"added      : {added}")
    print(f"duplicates : {duplicates}\n")

    # ── themes.csv 追記 ──────────────────────────────────
    csv_start = time.monotonic()
    if new_rows:
        try:
            append_themes(THEMES_CSV, new_rows)
            csv_dur     = time.monotonic() - csv_start
            stock_after = stock_before + added
            print("themes.csv updated")

            write_process_log(
                LOG_DIR, video_id="", process="csv_append",
                status="success",
                title=f"added={added} duplicates={duplicates}",
                duration_sec=csv_dur,
            )

        except Exception as e:
            csv_dur   = time.monotonic() - csv_start
            csv_error = str(e)
            print(f"[ERROR] CSV 書き込み失敗: {csv_error}")

            write_process_log(
                LOG_DIR, video_id="", process="csv_append",
                status="failed", title="themes.csv",
                duration_sec=csv_dur, error_message=csv_error,
            )
            write_theme_gen_log(
                LOG_DIR, run_id=run_id, status="failed",
                duration_sec=time.monotonic() - run_start,
                api_status=api_status, stock_before=stock_before,
                generated=len(generated), added=0, duplicates=duplicates,
                error_message=csv_error,
            )
            sys.exit(1)
    else:
        stock_after = stock_before
        csv_dur     = time.monotonic() - csv_start
        print("themes.csv : no new themes to add")

        write_process_log(
            LOG_DIR, video_dir="", process="csv_append",
            status="skipped",
            title=f"added=0 duplicates={duplicates}",
            duration_sec=csv_dur,
        )

    # ── サマリーログ ─────────────────────────────────────
    write_theme_gen_log(
        LOG_DIR, run_id=run_id, status="success",
        duration_sec=time.monotonic() - run_start,
        api_status=api_status,
        stock_before=stock_before, stock_after=stock_after,
        generated=len(generated), added=added, duplicates=duplicates,
    )


if __name__ == "__main__":
    main()
