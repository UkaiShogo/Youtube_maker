#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_genkou.py
原稿自動生成スクリプト
配置先: wikiproject\\scripts\\genkou_generater.py

【themes.csv status体系】
  0: 初期（theme_generator で作成）
  1: 原稿生成済み → genkou_generater.py で status=0→1 に更新
  2: 画像生成済み → image_generator.py で status=1→2 に更新
  3: 動画生成済み → movie_generater.py で status=1→3 に更新（status=2でも処理可）
  4: 投稿済み → youtube_uploader.py で status=3→4 に更新
  91: 原稿生成失敗スキップ ← genkou_generater.py でエラー時に status=91 を設定
  92: 画像生成失敗スキップ ← image_generator.py でリトライ超過時に status=92 を設定
  93: 動画生成失敗スキップ ← movie_generater.py でエラー時に status=93 を設定
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

from path_helper import (
    get_channel_data_dir,
    get_channel_config_dir,
    get_channel_prompts_dir,
    get_channel_logs_dir,
)

# ===== 設定 =====
ANTHROPIC_MODEL   = "claude-haiku-4-5-20251001"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
RETRY_COUNT    = 3   # エラー時のリトライ回数
RETRY_WAIT     = 5   # リトライ間隔（秒）

# log_writer・genkou_checker は同じ scripts/ ディレクトリに置く
sys.path.insert(0, str(Path(__file__).parent))
from log_writer import write_genkou_log, write_process_log
from genkou_checker import run_check, MAX_ATTEMPTS

JST = timezone(timedelta(hours=9))

# ===== Anthropic クライアント初期化 =====
import anthropic
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── 実行ID生成 ────────────────────────────────────────────
def make_run_id() -> str:
    return datetime.now(JST).strftime("%Y%m%d_%H%M%S")


# ── モデル確認 ────────────────────────────────────────────
def check_model() -> None:
    """起動時にモデルが使用可能か確認する"""
    print(f"モデル確認中: {ANTHROPIC_MODEL} ...", end=" ", flush=True)
    t_start = time.monotonic()
    try:
        # 最小トークンでモデルの疎通確認
        client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        t_dur = time.monotonic() - t_start
        print("OK\n")
        write_process_log(
            LOG_DIR, video_id="", process="model_check",
            status="success", title=ANTHROPIC_MODEL, duration_sec=t_dur,
        )
    except Exception as e:
        t_dur = time.monotonic() - t_start
        err   = str(e)
        print(f"\n\n[エラー] モデル '{ANTHROPIC_MODEL}' は使用できません。")
        print(f"詳細: {e}")
        print("\nスクリプト上部の ANTHROPIC_MODEL を確認してください。")

        write_process_log(
            LOG_DIR, video_id="", process="model_check",
            status="failed", title=ANTHROPIC_MODEL,
            duration_sec=t_dur, error_message=err,
        )
        raise SystemExit(1)


# ── プロンプト読み込み ────────────────────────────────────
def load_prompt_template(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── 原稿生成（リトライあり）──────────────────────────────
def generate_genkou(prompt: str) -> str:
    """Anthropic API を使って原稿を生成する（リトライあり）"""
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1000,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                ],
            )
            return response.content[0].text.strip()
        except Exception as e:
            if attempt < RETRY_COUNT:
                print(f"リトライ {attempt}/{RETRY_COUNT} ({e}) ... ", end="", flush=True)
                time.sleep(RETRY_WAIT)
            else:
                raise


# ── APIレスポンスのパース ────────────────────────────────────
def parse_genkou_response(raw: str) -> dict:
    """
    APIが返す "id,name,intro,body,comment" 形式の1行をパースして
    {"id","name","intro","body","comment"} の辞書を返す。
    """
    # コードブロックや空行を除去して最初の有効行を使用
    lines = [l.strip() for l in raw.strip().splitlines()
             if l.strip() and not l.strip().startswith("```")]
    if not lines:
        raise ValueError(f"APIレスポンスが空です: {repr(raw)}")
    target = lines[0]
    # id,name,intro,body,comment の5フィールドに分割（body内の|は保持）
    parts = target.split(",", maxsplit=4)
    if len(parts) != 5:
        raise ValueError(
            f"フィールド数不正（期待:5 実際:{len(parts)}）\n"
            f"レスポンス: {repr(target)}"
        )
    return {
        "id":      parts[0].strip(),
        "name":    parts[1].strip(),
        "intro":   parts[2].strip(),
        "body":    parts[3].strip(),
        "comment": parts[4].strip(),
    }


# ── genkou.csv 追記 ───────────────────────────────────────
def append_to_genkou_csv(path: str, rows: list[dict]) -> None:
    """生成結果を genkou.csv に追記する（1件ずつ即時書き込み）"""
    file_exists = os.path.isfile(path)
    fieldnames  = ["id", "name", "intro", "body", "comment"]
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


# ── themes.csv status 更新 ────────────────────────────────
def update_status(themes_path: str, processed_ids: set, new_status: str = "1") -> None:
    """
    処理済み行の status を更新して themes.csv を上書き保存する。

    Args:
        themes_path: themes.csv のパス
        processed_ids: 更新対象の id セット
        new_status: 設定する status 値（デフォルト "1"）
                   "1" = 原稿生成済み
                   "91" = 原稿生成失敗スキップ
    """
    rows = []
    with open(themes_path, "r", encoding="utf-8-sig", newline="") as f:
        reader     = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row["id"] in processed_ids:
                row["status"] = new_status
            rows.append(row)

    with open(themes_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ── メイン処理 ────────────────────────────────────────────
def main(channel: str = None, max_count: int = None):
    """
    Args:
        channel: チャンネル名（例: nazolabo）。None の場合はコマンドライン引数から取得
        max_count: 処理する原稿の最大件数。None の場合は status=0 の全件処理
                   （単体起動時のデフォルト）。manager.py から上限値を渡す。
    """
    # コマンドライン引数を解析（channel が None の場合のみ）
    if channel is None:
        parser = argparse.ArgumentParser(description="原稿自動生成")
        parser.add_argument("--channel", type=str, required=True, help="チャンネル名（例: nazolabo）")
        args = parser.parse_args()
        channel = args.channel

    # チャンネル別パスを取得
    DATA_DIR    = get_channel_data_dir(channel)
    CONFIG_DIR  = get_channel_config_dir(channel)
    PROMPTS_DIR = get_channel_prompts_dir(channel)
    LOG_DIR     = get_channel_logs_dir(channel)

    THEMES_CSV = str(DATA_DIR / "themes.csv")
    PROMPT_TXT = str(PROMPTS_DIR / "genkou_prompt.txt")
    GENKOU_CSV = str(DATA_DIR / "genkou.csv")

    run_id    = make_run_id()
    run_start = time.monotonic()

    # ── 0. モデル確認 ─────────────────────────────────────
    check_model()

    # ── 1. プロンプトテンプレート読み込み ─────────────────
    prompt_template = load_prompt_template(PROMPT_TXT)

    # ── 2. themes.csv から status=0 の行を抽出 ────────────
    pending_rows = []
    with open(THEMES_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        # ヘッダー検証（BOM残りやヘッダー欠落を検知）
        if reader.fieldnames is None or "status" not in reader.fieldnames:
            raise ValueError(
                f"themes.csv のヘッダーが不正です。"
                f"検出された列名: {reader.fieldnames}\n"
                f"正しい列名: id, name, themes, status"
            )
        for row in reader:
            if row.get("status", "").strip() == "0":
                pending_rows.append(row)

    # max_count が指定されている場合は先頭から上限件数に切り詰める
    if max_count is not None and len(pending_rows) > max_count:
        print(f"[設定] max_count={max_count} (manager指定) / 全件={len(pending_rows)} -> {max_count}件に絞り込み")
        pending_rows = pending_rows[:max_count]

    if not pending_rows:
        print("処理対象の行（status=0）が見つかりませんでした。")
        write_process_log(
            LOG_DIR, video_id="", process="genkou_generate",
            status="skipped", title="対象なし（status=0 の行が存在しない）",
        )
        write_genkou_log(
            LOG_DIR, run_id=run_id, status="no_target",
            duration_sec=time.monotonic() - run_start,
            api_status="skipped",
        )
        return

    target_count = len(pending_rows)
    print(f"処理対象: {target_count} 件\n")

    # ── hook_type に応じた intro生成指示 ──────────────────
    HOOK_INTRO_PROMPT = {
        "basic": "このテーマは『基本・現象の説明』タイプです。\n場所・見た目・基本的な性質を中心に「なぜ○○は□□なの？」という形で、\n主語を変えたり角度を変えてintroを作成してください。",
        "condition": "このテーマは『条件・タイミング』タイプです。\n「いつ」「どんなとき」という発生条件に注目して「なぜ○○は□□のとき△△なの？」\nという形でintroを作成してください。",
        "visual": "このテーマは『見た目・違和感』タイプです。\n「なぜそう見えるのか」「なぜ違う見え方をするのか」という視点で\n「なぜ○○は□□に見えるの？」という形でintroを作成してください。",
    }

    # ── 3. 1件ずつ原稿生成 ────────────────────────────────
    success_count = 0
    error_count   = 0

    for i, row in enumerate(pending_rows, 1):
        row_id     = row["id"]
        row_name   = row["name"]
        hook_type  = row.get("hook_type", "").strip() or "basic"  # デフォルトは basic
        label      = f"id={row_id}, name={row_name}, hook_type={hook_type}"
        hook_prompt = HOOK_INTRO_PROMPT.get(hook_type, HOOK_INTRO_PROMPT["basic"])
        prompt     = prompt_template.replace("{id}", row_id).replace("{name}", row_name).replace("{hook_type}", hook_prompt)

        print(f"[{i}/{target_count}] 生成中: {label} ...", end=" ", flush=True)
        item_start = time.monotonic()

        try:
            # API 呼び出し
            raw_response = generate_genkou(prompt)
            api_dur      = time.monotonic() - item_start

            write_process_log(
                LOG_DIR, video_id=row_id, process="genkou_generate",
                status="success", title=row_name, duration_sec=api_dur,
            )

            # 後処理チェック・修正・再生成（genkou_checker）
            def _regenerate():
                return generate_genkou(prompt)

            final_csv = run_check(
                raw_script    = raw_response,
                entry_id      = row_id,
                generate_func = _regenerate,
                log_dir       = LOG_DIR,
            )

            if final_csv is None:
                # 最大再生成回数超過 → status=91（原稿生成失敗スキップ）
                print(f"保留（チェック{MAX_ATTEMPTS}回失敗）")
                write_process_log(
                    LOG_DIR, video_id=row_id, process="genkou_check",
                    status="failed", title=row_name,
                    error_message="最大再生成回数超過・status=91設定",
                )
                # themes.csv status を 91 に更新
                update_status(THEMES_CSV, {row_id}, new_status="91")
                error_count += 1
                continue

            # チェック通過 → パースして保存
            parsed = parse_genkou_response(final_csv)

            # genkou.csv 即時追記（5列で保存）
            csv_start = time.monotonic()
            append_to_genkou_csv(GENKOU_CSV, [parsed])
            csv_dur = time.monotonic() - csv_start
            write_process_log(
                LOG_DIR, video_id=row_id, process="genkou_csv_write",
                status="success", title=row_name, duration_sec=csv_dur,
            )

            # themes.csv status 更新
            st_start = time.monotonic()
            update_status(THEMES_CSV, {row_id})
            st_dur = time.monotonic() - st_start
            write_process_log(
                LOG_DIR, video_id=row_id, process="status_update",
                status="success", title=row_name, duration_sec=st_dur,
            )

            success_count += 1
            print("完了")

        except Exception as e:
            item_dur  = time.monotonic() - item_start
            error_msg = str(e)
            error_count += 1
            print(f"失敗: {error_msg}")

            write_process_log(
                LOG_DIR, video_id=row_id, process="genkou_generate",
                status="failed", title=row_name,
                duration_sec=item_dur, error_message=error_msg,
            )

            # themes.csv status を 91 に更新（原稿生成失敗スキップ）
            try:
                update_status(THEMES_CSV, {row_id}, new_status="91")
            except Exception as update_e:
                print(f"  [警告] status 更新失敗: {update_e}")

    # ── 4. api_status の決定 ──────────────────────────────
    if error_count == 0:
        api_status = "success"
        run_status = "success"
    elif success_count == 0:
        api_status = "failed"
        run_status = "failed"
    else:
        api_status = "partial"
        run_status = "success"   # 一部成功でも全体はsuccess扱い

    # ── 5. サマリーログ ───────────────────────────────────
    write_genkou_log(
        LOG_DIR, run_id=run_id, status=run_status,
        duration_sec=time.monotonic() - run_start,
        api_status=api_status,
        target_count=target_count,
        success_count=success_count,
        error_count=error_count,
    )

    print(f"\n========== 処理完了 ==========")
    print(f"  成功: {success_count} 件")
    print(f"  失敗: {error_count} 件")
    print(f"================================")


if __name__ == "__main__":
    main()
