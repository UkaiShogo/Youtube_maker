"""
YouTube 自動アップロードスクリプト
配置先: wikiproject\\scripts\\youtube_uploader.py

【動作モード】
  uploader_config.ini の production = false  -> APIダミー（テスト確認のみ）
  uploader_config.ini の production = true   -> 本番（YouTube へ実際にアップロード）

【アップロード対象】
  output/result/*.mp4 のうち themes.csv に id+name が一致する行があるファイルのみ
  ファイル名フォーマット: {id}_{name}.mp4  例) 3_東京タワー.mp4

【予約投稿スケジュール】
  publish_hour の候補時刻を1本ずつ順番に割り当てる
  例) publish_hour=18,21、起点日=3/22 の場合
      1本目 -> 3/22 18:00 JST
      2本目 -> 3/22 21:00 JST
      3本目 -> 3/23 18:00 JST  ...

  起点日の決まり方:
    last_publish_date が空 -> 翌日（初回）
    last_publish_date に日付あり -> その翌日以降

  アップロード成功後:
    - config の last_publish_date を最後に割り当てた日付に自動更新
    - CSV の該当行 status を 4 に更新
    - logs/ 配下にログを出力（log_writer.py が必要）

【ログ出力】
  logs/youtube_uploader_log.csv  ... アップロード単位サマリー
  logs/process_log.csv           ... 処理ステップ詳細
"""

import argparse
import csv
import pickle
import shutil
import subprocess
import sys
import configparser
import textwrap
import time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

# scripts/ 配下のモジュールをインポート
sys.path.insert(0, str(Path(__file__).parent))
from path_helper import (
    get_channel_data_dir,
    get_channel_config_dir,
    get_channel_output_dir,
    get_channel_logs_dir,
    get_common_config_dir,
)
from log_writer import write_upload_log, write_process_log

JST = timezone(timedelta(hours=9))


# ============================================================
#  設定読み込み / 保存
# ============================================================
def load_config(config_path: Path) -> configparser.ConfigParser:
    if not config_path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {config_path}")
    cfg = configparser.ConfigParser()
    cfg.read(str(config_path), encoding="utf-8")
    return cfg


def save_last_publish_date(config_path: Path, last_date: date):
    """
    uploader_config.ini の [schedule] last_publish_date を上書き保存する。
    コメントを保持するためテキスト置換方式で処理する。
    """
    date_str = last_date.strftime("%Y-%m-%d")
    text = config_path.read_text(encoding="utf-8")
    new_lines = []
    for line in text.splitlines():
        if line.strip().startswith("last_publish_date"):
            new_lines.append(f"last_publish_date = {date_str}")
        else:
            new_lines.append(line)
    config_path.write_text("\n".join(new_lines), encoding="utf-8")


# ============================================================
#  予約スケジュール生成
# ============================================================
def build_schedule(publish_hours: list[int], last_publish_date_str: str,
                   count: int) -> list[tuple[datetime, str]]:
    """
    count 本分の予約日時リストを生成して返す。
    各要素は (datetime_jst, utc_iso8601_str)。

    起点日:
      last_publish_date_str が空  -> 今日の翌日
      last_publish_date_str に値  -> その日付の翌日

    割り当てロジック（hours=[18,21]、起点=3/22 の場合）:
      0本目 -> 3/22 18:00 JST
      1本目 -> 3/22 21:00 JST
      2本目 -> 3/23 18:00 JST ...
    """
    if last_publish_date_str.strip():
        base_date  = datetime.strptime(last_publish_date_str.strip(), "%Y-%m-%d").date()
        start_date = base_date + timedelta(days=1)
    else:
        start_date = datetime.now(JST).date() + timedelta(days=1)

    schedule = []
    for i in range(count):
        day_offset  = i // len(publish_hours)
        hour_index  = i %  len(publish_hours)
        target_date = start_date + timedelta(days=day_offset)
        h           = publish_hours[hour_index]
        dt_jst      = datetime(target_date.year, target_date.month, target_date.day,
                               h, 0, 0, tzinfo=JST)
        dt_utc_str  = dt_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        schedule.append((dt_jst, dt_utc_str))
    return schedule


# ============================================================
#  認証 / ダミーサービス
# ============================================================
def get_authenticated_service(test_mode: bool, token_path: Path):
    if test_mode:
        print("  [TEST MODE] YouTube API 認証をスキップします。")
        return DummyYouTube()

    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request

    if not token_path.exists():
        raise FileNotFoundError(f"token.pickle が見つかりません: {token_path}")

    with open(token_path, "rb") as f:
        creds = pickle.load(f)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)
        print("  ✔ トークンをリフレッシュしました。")

    return build("youtube", "v3", credentials=creds)


class DummyYouTube:
    """テストモード用スタブ。API は一切呼ばない。"""
    def videos(self):
        return self
    def insert(self, **kwargs):
        return DummyRequest()


class DummyRequest:
    _call_count = 0
    def next_chunk(self):
        self._call_count += 1
        if self._call_count == 1:
            return DummyStatus(0.5), None
        return None, {"id": "DUMMY_VIDEO_ID_TEST"}


class DummyStatus:
    def __init__(self, pct):
        self._pct = pct
    def progress(self):
        return self._pct


# ============================================================
#  CSV 操作
# ============================================================
def load_csv(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def save_csv(csv_path: Path, rows: list[dict]):
    # fieldnames は実際のデータから取得（列の追加・変更に追従）
    fieldnames = list(rows[0].keys()) if rows else ["id", "name", "themes", "image", "status"]
    tmp_path = csv_path.with_suffix(".tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    shutil.move(str(tmp_path), str(csv_path))


def parse_filename(stem: str) -> tuple[str, str] | None:
    """
    ファイル名（拡張子なし）を {id}_{name} 形式でパースし (id, name) を返す。
    例) "3_東京タワー" -> ("3", "東京タワー")
    """
    parts = stem.split("_", maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return parts[0], parts[1]


def update_csv_status(csv_path: Path, rows: list[dict],
                      file_id: str, file_name: str, new_status: int = 4) -> bool:
    """CSV の id と name が両方一致する行の status を new_status に更新する。"""
    updated = False
    for row in rows:
        if row["id"].strip() == file_id and row["name"].strip() == file_name:
            row["status"] = str(new_status)
            updated = True
            break
    if updated:
        save_csv(csv_path, rows)
    return updated


# ============================================================
#  メタデータ組み立て
# ============================================================
def build_description(template: str, themes: str) -> str:
    return textwrap.dedent(template).strip().replace("{themes}", themes)


def build_tags(tags_base_raw: str, themes: str) -> list[str]:
    base    = [t.strip() for t in tags_base_raw.split(",") if t.strip()]
    dynamic = [themes.strip()] if themes.strip() else []
    return base + dynamic


def build_video_body(cfg: configparser.ConfigParser,
                     title: str, themes: str, publish_at_utc: str) -> dict:
    category_id        = cfg.get("video", "category_id",                       fallback="27")
    default_language   = cfg.get("video", "default_language",                  fallback="ja")
    default_audio_lang = cfg.get("video", "default_audio_language",            fallback="ja")
    privacy_status     = cfg.get("video", "privacy_status",                    fallback="private")
    made_for_kids      = cfg.getboolean("video", "self_declared_made_for_kids", fallback=False)
    public_stats       = cfg.getboolean("video", "public_stats_viewable",       fallback=True)
    embeddable         = cfg.getboolean("video", "embeddable",                  fallback=True)
    license_val        = cfg.get("video", "license",                            fallback="youtube")
    tags_base_raw      = cfg.get("video", "tags_base",                          fallback="")
    desc_template      = cfg.get("description", "template",                     fallback="{themes}")

    return {
        "snippet": {
            "title":                title,
            "description":          build_description(desc_template, themes),
            "tags":                 build_tags(tags_base_raw, themes),
            "categoryId":           category_id,
            "defaultLanguage":      default_language,
            "defaultAudioLanguage": default_audio_lang,
        },
        "status": {
            "privacyStatus":           privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
            "publicStatsViewable":     public_stats,
            "embeddable":              embeddable,
            "license":                 license_val,
            "publishAt":               publish_at_utc,
        },
    }


# ============================================================
#  YouTube アップロード
# ============================================================
def upload_video(youtube, video_path: Path, body: dict, test_mode: bool) -> str:
    """動画をアップロードして動画IDを返す。"""
    if test_mode:
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=None)
    else:
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(
            str(video_path), mimetype="video/mp4",
            resumable=True, chunksize=1024 * 1024 * 8)
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=media)

    label = "[DUMMY] シミュレーション" if test_mode else "アップロード中"
    print(f"  {label}: {video_path.name} ...")

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    進捗: {int(status.progress() * 100)}%", end="\r")

    video_id   = response["id"]
    done_label = "[DUMMY] 完了（実際には未アップロード）" if test_mode else "アップロード完了"
    print(f"  ✔ {done_label}  動画ID: {video_id}")
    return video_id


# ============================================================
#  メタデータ確認ログ（コンソール表示）
# ============================================================
def print_meta(body: dict, publish_at_jst: datetime):
    s, st = body["snippet"], body["status"]
    print(f"  タイトル          : {s['title']}")
    print(f"  カテゴリID        : {s['categoryId']}")
    print(f"  言語              : {s['defaultLanguage']} / 音声: {s['defaultAudioLanguage']}")
    print(f"  タグ              : {', '.join(s['tags'])}")
    print(f"  公開設定          : {st['privacyStatus']}")
    print(f"  予約公開(JST)     : {publish_at_jst.strftime('%Y-%m-%d %H:%M JST')}")
    print(f"  予約公開(UTC)     : {st['publishAt']}")
    print(f"  子ども向け        : {st['selfDeclaredMadeForKids']}")
    print(f"  統計公開          : {st['publicStatsViewable']}")
    print(f"  埋め込み          : {st['embeddable']}")
    print(f"  ライセンス        : {st['license']}")
    print(f"  説明文            : {s['description'].replace(chr(10), ' / ')}")


# ============================================================
#  メイン処理
# ============================================================
def main(channel: str = None, max_count: int = None):
    """
    Args:
        channel: チャンネル名（例: nazolabo）。None の場合はコマンドライン引数から取得
        max_count: 投稿する動画の最大本数。None の場合は targets 全件を処理
                   （単体起動時のデフォルト）。manager.py から上限値を渡す。
    """
    # コマンドライン引数を解析（channel が None の場合のみ）
    if channel is None:
        parser = argparse.ArgumentParser(description="YouTube 自動アップロード")
        parser.add_argument("--channel", type=str, required=True, help="チャンネル名（例: nazolabo）")
        args = parser.parse_args()
        channel = args.channel

    # チャンネル別パスを取得
    DATA_DIR   = get_channel_data_dir(channel)
    OUTPUT_DIR = get_channel_output_dir(channel)
    LOG_DIR    = get_channel_logs_dir(channel)
    CONFIG_DIR = get_channel_config_dir(channel)
    COMMON_CONFIG_DIR = get_common_config_dir()

    VIDEO_DIR    = OUTPUT_DIR / "result"
    UPLOADED_DIR = OUTPUT_DIR / "uploaded"
    CSV_PATH     = DATA_DIR / "themes.csv"
    TOKEN_PATH   = COMMON_CONFIG_DIR / "token.pickle"
    CONFIG_PATH  = CONFIG_DIR / "uploader_config.ini"

    # --- 設定読み込み ---
    cfg           = load_config(CONFIG_PATH)
    is_production = cfg.getboolean("general", "production", fallback=False)
    test_mode     = not is_production

    # --- パス上書き ---
    video_dir    = Path(cfg.get("paths", "video_dir",    fallback=str(VIDEO_DIR)))
    uploaded_dir = Path(cfg.get("paths", "uploaded_dir", fallback=str(UPLOADED_DIR)))
    csv_path     = Path(cfg.get("paths", "csv_path",     fallback=str(CSV_PATH)))
    token_path   = Path(cfg.get("paths", "token_path",   fallback=str(TOKEN_PATH)))
    log_dir      = Path(cfg.get("paths", "log_dir",      fallback=str(LOG_DIR)))

    # --- スケジュール設定 ---
    hours_raw         = cfg.get("schedule", "publish_hour",      fallback="18,21")
    last_publish_date = cfg.get("schedule", "last_publish_date", fallback="").strip()
    publish_hours     = [int(h.strip()) for h in hours_raw.split(",") if h.strip()]

    # --- ヘッダー表示 ---
    print("=" * 60)
    print(f"  YouTube 自動アップロード  "
          f"[{'★ TEST MODE ★' if test_mode else '本番モード'}]")
    print("=" * 60)
    if test_mode:
        print("  ⚠ production = false のため、実際にはアップロードされません。")
        print("    本番実行: uploader_config.ini の production を true に変更してください。\n")

    start_from = (
        last_publish_date if last_publish_date
        else f"未設定（翌日 = {(datetime.now(JST).date() + timedelta(days=1)).strftime('%Y-%m-%d')} から）"
    )
    print(f"  前回最終公開予定日: {start_from}")
    print(f"  公開候補時刻(JST) : {', '.join(str(h) + ':00' for h in publish_hours)}\n")

    # --- CSV 読み込み ---
    if not csv_path.exists():
        print(f"✖ CSVが見つかりません: {csv_path}")
        return
    csv_rows = load_csv(csv_path)
    csv_map: dict[tuple[str, str], str] = {
        (row["id"].strip(), row["name"].strip()): row["themes"].strip()
        for row in csv_rows
    }
    print(f"CSV 登録件数: {len(csv_rows)} 件\n")

    # --- mp4 一覧取得 ---
    if not video_dir.exists():
        print(f"✖ 動画フォルダが見つかりません: {video_dir}")
        return
    mp4_files = sorted(video_dir.glob("*.mp4"))
    if not mp4_files:
        print(f"⚠ mp4ファイルが見つかりません: {video_dir}")
        return

    # --- ファイル名パース & CSV照合 ---
    targets: list[tuple[Path, str, str, str]] = []  # (path, id, name, themes)
    skipped: list[tuple[Path, str]] = []

    for f in mp4_files:
        parsed = parse_filename(f.stem)
        if parsed is None:
            reason = "ファイル名が {id}_{name} 形式ではない"
            skipped.append((f, reason))
            write_process_log(log_dir, video_id=f.name, process="skip",
                              status="skipped", title=f.stem, error_message=reason)
            continue
        fid, fname = parsed
        themes = csv_map.get((fid, fname))
        if themes is None:
            reason = f"CSV に id={fid}, name={fname} が存在しない"
            skipped.append((f, reason))
            write_process_log(log_dir, video_id=f.name, process="skip",
                              status="skipped", title=fname, error_message=reason)
        else:
            targets.append((f, fid, fname, themes))

    print(f"mp4 ファイル総数        : {len(mp4_files)} 件")
    print(f"  アップロード対象      : {len(targets)} 件（CSV の id+name に一致）")
    print(f"  スキップ              : {len(skipped)} 件")
    if skipped:
        print("  【スキップ一覧】")
        for f, reason in skipped:
            print(f"    - {f.name}  <- {reason}")
    print()

    if not targets:
        print("アップロード対象がありません。処理を終了します。")
        return

    # max_count が指定されている場合は先頭から上限本数に切り詰める
    if max_count is not None and len(targets) > max_count:
        print(f"  [設定] max_count={max_count} (manager指定) / 全件={len(targets)} -> {max_count}件に絞り込み")
        targets = targets[:max_count]

    # --- 予約スケジュール生成 ---
    schedule = build_schedule(publish_hours, last_publish_date, len(targets))

    print("【予約スケジュール（割り当て予定）】")
    for i, ((dt_jst, _), (_, _, fname, _)) in enumerate(zip(schedule, targets)):
        print(f"  {i+1}本目: {dt_jst.strftime('%Y-%m-%d %H:%M JST')}  {fname}")
    print()

    # --- 認証 ---
    youtube = get_authenticated_service(test_mode, token_path)
    if not test_mode:
        print("✔ YouTube API 認証成功\n")

    # --- アップロードループ ---
    success_count   = 0
    fail_count      = 0
    last_success_dt: datetime | None = None

    for (video_path, file_id, file_name, themes), (dt_jst, publish_at_utc) in zip(targets, schedule):
        print(f"[処理] {video_path.name}")

        body = build_video_body(cfg, title=file_name, themes=themes,
                                publish_at_utc=publish_at_utc)
        print_meta(body, dt_jst)

        # --- アップロード ---
        upload_start = time.monotonic()
        video_id     = video_path.name  # 失敗時のフォールバック用
        error_msg    = ""

        try:
            video_id = upload_video(youtube, video_path, body, test_mode)
            upload_duration = time.monotonic() - upload_start

            # process_log: upload success
            write_process_log(
                log_dir, video_id=video_id, process="upload",
                status="success", title=file_name,
                duration_sec=upload_duration,
            )

            # --- CSV 更新 ---
            if test_mode:
                print("  [DUMMY] CSV 更新・config 更新・ファイル移動はスキップ（test_mode = true）")
            else:
                csv_start = time.monotonic()
                updated = update_csv_status(csv_path, csv_rows, file_id, file_name, new_status=4)
                csv_duration = time.monotonic() - csv_start

                if updated:
                    print("  ✔ CSV更新完了 (status -> 4)")
                    write_process_log(
                        log_dir, video_id=video_id, process="csv_update",
                        status="success", title=file_name, duration_sec=csv_duration,
                    )

                    # --- youtube_logger.py --mode upload を呼び出す ---
                    logger_start = time.monotonic()
                    try:
                        # CSV行から channel, theme_id を取得
                        channel = "nazewhy"  # デフォルト値
                        theme_id = file_id
                        for row in csv_rows:
                            if row.get("id", "").strip() == file_id and row.get("name", "").strip() == file_name:
                                channel = row.get("channel", "nazewhy").strip()
                                theme_id = row.get("id", file_id).strip()
                                break

                        # youtube_logger.py を呼び出し
                        logger_cmd = [
                            "python", str(Path(__file__).parent / "youtube_logger.py"),
                            "--mode", "upload",
                            "--video_id", video_id,
                            "--channel", channel,
                            "--theme_id", theme_id,
                            "--title", file_name,
                        ]
                        result = subprocess.run(logger_cmd, capture_output=True, text=True, timeout=30)
                        logger_duration = time.monotonic() - logger_start

                        if result.returncode == 0:
                            print("  ✔ youtube_logger 記録完了")
                            write_process_log(
                                log_dir, video_id=video_id, process="logger_upload",
                                status="success", title=file_name, duration_sec=logger_duration,
                            )
                        else:
                            print(f"  ⚠ youtube_logger 記録失敗: {result.stderr}")
                            write_process_log(
                                log_dir, video_id=video_id, process="logger_upload",
                                status="failed", title=file_name,
                                duration_sec=logger_duration, error_message=result.stderr,
                            )
                    except subprocess.TimeoutExpired:
                        logger_duration = time.monotonic() - logger_start
                        print("  ⚠ youtube_logger タイムアウト")
                        write_process_log(
                            log_dir, video_id=video_id, process="logger_upload",
                            status="failed", title=file_name,
                            duration_sec=logger_duration, error_message="Timeout",
                        )
                    except Exception as logger_err:
                        logger_duration = time.monotonic() - logger_start
                        print(f"  ⚠ youtube_logger エラー: {logger_err}")
                        write_process_log(
                            log_dir, video_id=video_id, process="logger_upload",
                            status="failed", title=file_name,
                            duration_sec=logger_duration, error_message=str(logger_err),
                        )
                else:
                    msg = f"CSV 更新失敗: id={file_id}, name={file_name}"
                    print(f"  ⚠ {msg}")
                    write_process_log(
                        log_dir, video_id=video_id, process="csv_update",
                        status="failed", title=file_name,
                        duration_sec=csv_duration, error_message=msg,
                    )

                # --- 動画ファイルを uploaded フォルダへ移動 ---
                move_start = time.monotonic()
                try:
                    uploaded_dir.mkdir(parents=True, exist_ok=True)
                    dest = uploaded_dir / video_path.name
                    shutil.move(str(video_path), str(dest))
                    move_duration = time.monotonic() - move_start
                    print(f"  ✔ ファイル移動完了: {video_path.name} -> uploaded/")
                    write_process_log(
                        log_dir, video_id=video_id, process="file_move",
                        status="success", title=file_name, duration_sec=move_duration,
                    )
                except Exception as move_err:
                    move_duration = time.monotonic() - move_start
                    print(f"  ⚠ ファイル移動失敗: {move_err}")
                    write_process_log(
                        log_dir, video_id=video_id, process="file_move",
                        status="failed", title=file_name,
                        duration_sec=move_duration, error_message=str(move_err),
                    )

                last_success_dt = dt_jst

            # upload_log: success
            write_upload_log(
                log_dir, video_id=video_id, title=file_name,
                status="success", duration_sec=upload_duration,
                scheduled_time_jst=dt_jst,
            )
            success_count += 1

        except Exception as e:
            upload_duration = time.monotonic() - upload_start
            error_msg = str(e)
            print(f"  ✖ エラー: {error_msg}")

            # process_log: upload failed
            write_process_log(
                log_dir, video_id=video_id, process="upload",
                status="failed", title=file_name,
                duration_sec=upload_duration, error_message=error_msg,
            )
            # upload_log: failed
            write_upload_log(
                log_dir, video_id=video_id, title=file_name,
                status="failed", duration_sec=upload_duration,
                scheduled_time_jst=dt_jst, error_message=error_msg,
            )
            fail_count += 1

        print()

    # --- config の last_publish_date を更新 ---
    if last_success_dt is not None:
        new_last_date = last_success_dt.date()
        cfg_start = time.monotonic()
        save_last_publish_date(CONFIG_PATH, new_last_date)
        cfg_duration = time.monotonic() - cfg_start

        write_process_log(
            log_dir, video_id="", process="config_update",
            status="success",
            title=f"last_publish_date -> {new_last_date.strftime('%Y-%m-%d')}",
            duration_sec=cfg_duration,
        )
        print(f"✔ config 更新: last_publish_date = {new_last_date.strftime('%Y-%m-%d')}\n")

    # --- サマリー ---
    print("=" * 60)
    suffix = "  ※ TEST MODE（実際には未アップロード）" if test_mode else ""
    print(f"  処理完了  成功: {success_count} / 失敗: {fail_count}{suffix}")
    print(f"  ログ出力先: {log_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
