"""
ログ書き込みモジュール
配置先: wikiproject\\scripts\\log_writer.py

【出力ファイル】
  youtube_uploader_log.csv  ... アップロード単位のサマリーログ
  theme_generator_log.csv   ... テーマ生成実行単位のサマリーログ
  process_log.csv           ... 処理ステップ単位の詳細ログ（全スクリプト共通）
"""

import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ログディレクトリ（デフォルト）
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# ============================================================
#  共通ユーティリティ
# ============================================================

def _now_jst() -> str:
    """現在時刻を JST で "YYYY-MM-DD HH:MM:SS" 形式で返す。"""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_log_file(log_path: Path, fieldnames: list[str]):
    """ログファイルが存在しない場合、ディレクトリとヘッダー行を作成する。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()


def _append_row(log_path: Path, fieldnames: list[str], row: dict):
    """ログ CSV に1行追記する。"""
    _ensure_log_file(log_path, fieldnames)
    with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerow({k: row.get(k, "") for k in fieldnames})


# ============================================================
#  youtube_uploader_log.csv
#    アップロード1本ごとのサマリーログ
# ============================================================
UPLOAD_LOG_FIELDS = [
    "timestamp",        # 実行時刻 (JST)
    "video_id",         # YouTube 動画ID（またはファイル名）
    "title",            # 動画タイトル
    "status",           # success / failed
    "duration",         # 処理時間（秒、小数点2桁）
    "scheduled_time",   # 公開予定時刻 (JST)
    "error_message",    # 失敗時のみ
]


def write_upload_log(
    log_dir: Path,
    video_id: str,
    title: str,
    status: str,           # "success" or "failed"
    duration_sec: float,
    scheduled_time_jst: datetime,
    error_message: str = "",
):
    """
    youtube_uploader_log.csv に1行追記する。

    Args:
        log_dir:            ログ出力ディレクトリ
        video_id:           YouTube 動画ID（失敗時はファイル名を渡す）
        title:              動画タイトル
        status:             "success" または "failed"
        duration_sec:       処理時間（秒）
        scheduled_time_jst: 公開予定日時（JST datetime オブジェクト）
        error_message:      エラーメッセージ（成功時は空文字）
    """
    log_path = log_dir / "youtube_uploader_log.csv"
    row = {
        "timestamp":      _now_jst(),
        "video_id":       video_id,
        "title":          title,
        "status":         status,
        "duration":       f"{duration_sec:.2f}",
        "scheduled_time": scheduled_time_jst.strftime("%Y-%m-%d %H:%M JST"),
        "error_message":  error_message,
    }
    _append_row(log_path, UPLOAD_LOG_FIELDS, row)


# ============================================================
#  process_log.csv
#    処理ステップ単位の詳細ログ
# ============================================================
PROCESS_LOG_FIELDS = [
    "timestamp",      # 実行時刻 (JST)
    "video_id",       # YouTube 動画ID（またはファイル名）
    "process",        # 処理ステップ名
    "status",         # success / failed / skipped
    "title",          # 動画タイトル
    "duration",       # 処理時間（秒、小数点2桁）
    "error_message",  # 失敗時のみ
]


def write_process_log(
    log_dir: Path,
    video_id: str,
    process: str,
    status: str,
    title: str = "",
    duration_sec: float = 0.0,
    error_message: str = "",
):
    """
    process_log.csv に1行追記する。

    process の例:
      "upload"           ... YouTube アップロード
      "csv_update"       ... themes.csv の status 更新
      "config_update"    ... uploader_config.ini の last_publish_date 更新
      "skip"             ... ファイルスキップ
      "threshold_check"  ... テーマ在庫の閾値チェック
      "api_call"         ... OpenAI API 呼び出し
      "csv_append"       ... themes.csv へのテーマ追記
      "model_check"      ... OpenAI モデル疎通確認
      "genkou_generate"  ... 原稿1件の生成
      "genkou_csv_write" ... genkou.csv への1件書き込み
      "status_update"    ... themes.csv の status 更新（generate_genkou）
      "voicevox_check"   ... VOICEVOX 起動確認
      "asset_check"      ... 背景・キャラ画像アセット存在確認
      "audio_generate"   ... 音声生成（全セクション）
      "clip_generate"    ... クリップ生成（全セクション）
      "video_render"     ... 最終動画レンダリング（結合・BGMミックス）
      "themes_update"    ... themes.csv の status 更新（movie_gen）
    """
    log_path = log_dir / "process_log.csv"
    row = {
        "timestamp":     _now_jst(),
        "video_id":      video_id,
        "process":       process,
        "status":        status,
        "title":         title,
        "duration":      f"{duration_sec:.2f}",
        "error_message": error_message,
    }
    _append_row(log_path, PROCESS_LOG_FIELDS, row)


# ============================================================
#  theme_generator_log.csv
#    テーマ生成1回の実行単位サマリーログ
# ============================================================
THEME_GEN_LOG_FIELDS = [
    "timestamp",      # 実行時刻 (JST)
    "run_id",         # 実行識別子（YYYYMMDD_HHMMSS）
    "status",         # success / failed / skipped
    "duration",       # 実行全体の処理時間（秒、小数点2桁）
    "error_message",  # 失敗時のみ
    "api_status",     # OpenAI API の結果: success / failed / skipped
    "stock_before",   # 実行前の status=0 件数
    "stock_after",    # 実行後の status=0 件数（追記後）
    "generated",      # API が返したテーマ数
    "added",          # 重複除去後に実際に追記した数
    "duplicates",     # 重複としてスキップした数
]


def write_theme_gen_log(
    log_dir: Path,
    run_id: str,
    status: str,
    duration_sec: float,
    api_status: str,
    stock_before: int,
    stock_after: int  = 0,
    generated: int    = 0,
    added: int        = 0,
    duplicates: int   = 0,
    error_message: str = "",
):
    """
    theme_generator_log.csv に1行追記する。

    Args:
        log_dir:       ログ出力ディレクトリ
        run_id:        実行識別子（YYYYMMDD_HHMMSS 形式を推奨）
        status:        "success" / "failed" / "skipped"（閾値未達で実行不要の場合）
        duration_sec:  実行全体の処理時間（秒）
        api_status:    "success" / "failed" / "skipped"
        stock_before:  実行前の status=0 件数
        stock_after:   実行後の status=0 件数
        generated:     API が返したテーマ数
        added:         重複除去後に追記した数
        duplicates:    重複としてスキップした数
        error_message: エラーメッセージ（成功時は空文字）
    """
    log_path = log_dir / "theme_generator_log.csv"
    row = {
        "timestamp":    _now_jst(),
        "run_id":       run_id,
        "status":       status,
        "duration":     f"{duration_sec:.2f}",
        "error_message": error_message,
        "api_status":   api_status,
        "stock_before": stock_before,
        "stock_after":  stock_after,
        "generated":    generated,
        "added":        added,
        "duplicates":   duplicates,
    }
    _append_row(log_path, THEME_GEN_LOG_FIELDS, row)


# ============================================================
#  generate_genkou_log.csv
#    原稿生成1回の実行単位サマリーログ
# ============================================================
GENKOU_LOG_FIELDS = [
    "timestamp",      # 実行時刻 (JST)
    "run_id",         # 実行識別子（YYYYMMDD_HHMMSS）
    "status",         # success / failed / no_target
    "duration",       # 実行全体の処理時間（秒、小数点2桁）
    "error_message",  # 実行全体で致命的エラーが発生した場合
    "api_status",     # success / partial / failed
    "target_count",   # 処理対象件数（status=0 の行数）
    "success_count",  # 成功件数
    "error_count",    # 失敗件数
]


def write_genkou_log(
    log_dir: Path,
    run_id: str,
    status: str,
    duration_sec: float,
    api_status: str,
    target_count: int  = 0,
    success_count: int = 0,
    error_count: int   = 0,
    error_message: str = "",
):
    """
    generate_genkou_log.csv に1行追記する。

    Args:
        log_dir:       ログ出力ディレクトリ
        run_id:        実行識別子（YYYYMMDD_HHMMSS 形式を推奨）
        status:        "success" / "failed" / "no_target"（対象なし）
        duration_sec:  実行全体の処理時間（秒）
        api_status:    "success"（全件成功）/ "partial"（一部失敗）/ "failed"（全件失敗）
        target_count:  処理対象件数
        success_count: 成功件数
        error_count:   失敗件数
        error_message: 致命的エラーのメッセージ（通常は空文字）
    """
    log_path = log_dir / "generate_genkou_log.csv"
    row = {
        "timestamp":     _now_jst(),
        "run_id":        run_id,
        "status":        status,
        "duration":      f"{duration_sec:.2f}",
        "error_message": error_message,
        "api_status":    api_status,
        "target_count":  target_count,
        "success_count": success_count,
        "error_count":   error_count,
    }
    _append_row(log_path, GENKOU_LOG_FIELDS, row)


# ============================================================
#  movie_gen_log.csv
#    動画生成1本ごとのサマリーログ
# ============================================================
MOVIE_GEN_LOG_FIELDS = [
    "timestamp",          # 実行時刻 (JST)
    "video_id",           # エントリID
    "title",              # エントリ名（name）
    "status",             # success / failed
    "duration",           # 処理時間（秒、小数点2桁）
    "voicevox_status",    # ok / error（未起動・接続失敗）
    "audio_duration",     # 生成した音声の合計時間（秒、小数点2桁）
    "asset_missing_flag", # none / bg / chara / bg+chara（不足アセット種別）
    "render_status",      # success / failed
    "error_message",      # 失敗時のみ
]


def write_movie_gen_log(
    log_dir: Path,
    video_id: str,
    title: str,
    status: str,
    duration_sec: float,
    voicevox_status: str,
    audio_duration: float       = 0.0,
    asset_missing_flag: str     = "none",
    render_status: str          = "success",
    error_message: str          = "",
):
    """
    movie_gen_log.csv に1行追記する。

    Args:
        log_dir:             ログ出力ディレクトリ
        video_id:            エントリID
        title:               エントリ名（name）
        status:              "success" / "failed"
        duration_sec:        動画1本の処理時間（秒）
        voicevox_status:     "ok" / "error"
        audio_duration:      全セクション音声の合計時間（秒）
        asset_missing_flag:  "none" / "bg" / "chara" / "bg+chara"
        render_status:       "success" / "failed"
        error_message:       エラーメッセージ（成功時は空文字）
    """
    log_path = log_dir / "movie_gen_log.csv"
    row = {
        "timestamp":          _now_jst(),
        "video_id":           video_id,
        "title":              title,
        "status":             status,
        "duration":           f"{duration_sec:.2f}",
        "voicevox_status":    voicevox_status,
        "audio_duration":     f"{audio_duration:.2f}",
        "asset_missing_flag": asset_missing_flag,
        "render_status":      render_status,
        "error_message":      error_message,
    }
    _append_row(log_path, MOVIE_GEN_LOG_FIELDS, row)


# ============================================================
#  manager_log.csv
#    manager.py の実行単位サマリーログ
# ============================================================
MANAGER_LOG_FIELDS = [
    "timestamp",           # 実行時刻 (JST)
    "run_id",              # 実行識別子（YYYYMMDD_HHMMSS）
    "status",              # success / failed / skipped
    "duration",            # 実行全体の処理時間（秒、小数点2桁）
    "step_theme",          # skipped / success / failed
    "step_genkou",         # skipped / success / failed
    "step_movie",          # skipped / success / failed
    "step_upload",         # skipped / success / failed
    "stock_theme_before",  # 実行前 status=0 件数
    "stock_genkou_before", # 実行前 status=1 件数
    "stock_movie_before",  # 実行前 status=2 件数
    "error_message",       # 失敗時のみ
]


def write_manager_log(
    log_dir: Path,
    run_id: str,
    status: str,
    duration_sec: float,
    step_theme: str          = "skipped",
    step_genkou: str         = "skipped",
    step_movie: str          = "skipped",
    step_upload: str         = "skipped",
    stock_theme_before: int  = 0,
    stock_genkou_before: int = 0,
    stock_movie_before: int  = 0,
    error_message: str       = "",
):
    """manager_log.csv に1行追記する。"""
    log_path = log_dir / "manager_log.csv"
    row = {
        "timestamp":           _now_jst(),
        "run_id":              run_id,
        "status":              status,
        "duration":            f"{duration_sec:.2f}",
        "step_theme":          step_theme,
        "step_genkou":         step_genkou,
        "step_movie":          step_movie,
        "step_upload":         step_upload,
        "stock_theme_before":  stock_theme_before,
        "stock_genkou_before": stock_genkou_before,
        "stock_movie_before":  stock_movie_before,
        "error_message":       error_message,
    }
    _append_row(log_path, MANAGER_LOG_FIELDS, row)


# ============================================================
#  genkou_check_log.csv
#    台本後処理チェック1件ごとのログ
# ============================================================
GENKOU_CHECK_LOG_FIELDS = [
    "timestamp",       # 処理時刻 (JST・ISO形式)
    "id",              # 台本ID
    "attempt",         # 生成回数（1〜3）
    "status",          # ok / fixed / rejected
    "reject_reason",   # rejected の場合の理由（軽微修正の場合は空）
    "errors",          # 検出したエラー一覧（セミコロン区切り）
    "fixes",           # 適用した修正一覧（セミコロン区切り）
    "intro_length",    # intro の文字数
    "body_lengths",    # body 各区切りの文字数（カンマ区切り 例:15,13,16,14）
    "comment_length",  # comment の文字数
    "raw_script",      # 生成直後の生の台本CSV文字列
    "final_script",    # 最終採用した台本CSV文字列（rejected の場合は空）
    "ng_words_hit",    # ヒットしたNGワード（セミコロン区切り、なければ空）
]


def write_genkou_check_log(
    log_dir: Path,
    entry_id: str,
    attempt: int,
    status: str,
    reject_reason: str  = "",
    errors: list        = None,
    fixes: list         = None,
    intro_length: int   = 0,
    body_lengths: list  = None,
    comment_length: int = 0,
    raw_script: str     = "",
    final_script: str   = "",
    ng_words_hit: list  = None,
):
    """
    genkou_check_log.csv に1行追記する。

    Args:
        log_dir:        ログ出力ディレクトリ
        entry_id:       台本ID
        attempt:        生成回数（1始まり）
        status:         "ok" / "fixed" / "rejected"
        reject_reason:  重度エラーの理由（ok/fixed は空文字）
        errors:         検出したエラーのリスト
        fixes:          適用した修正のリスト
        intro_length:   intro の文字数
        body_lengths:   body 各区切りの文字数リスト [15, 13, 16, 14]
        comment_length: comment の文字数
        raw_script:     生成直後の生の台本CSV文字列
        final_script:   最終採用台本（rejected の場合は空文字）
        ng_words_hit:   ヒットしたNGワードのリスト（なければ空リスト）
    """
    log_path = log_dir / "genkou_check_log.csv"
    row = {
        "timestamp":      datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S"),
        "id":             entry_id,
        "attempt":        attempt,
        "status":         status,
        "reject_reason":  reject_reason,
        "errors":         ";".join(errors or []),
        "fixes":          ";".join(fixes or []),
        "intro_length":   intro_length,
        "body_lengths":   ",".join(str(x) for x in (body_lengths or [])),
        "comment_length": comment_length,
        "raw_script":     raw_script,
        "final_script":   final_script,
        "ng_words_hit":   ";".join(ng_words_hit or []),
    }
    _append_row(log_path, GENKOU_CHECK_LOG_FIELDS, row)
