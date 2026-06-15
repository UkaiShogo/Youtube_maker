#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genkou_checker.py
台本後処理チェック・修正・再生成モジュール

【処理フロー】
  ① 台本CSV文字列を受け取る
  ② 自動チェック
  ③ 分岐
     ├ OK      → そのまま採用
     ├ 軽微    → 自動修正して採用
     └ 重度    → 再生成（最大3回）

【チェック仕様】
  フォーマット : カンマ数4個・body区切り4個       → 重度
  文字数       : intro 12〜20 / body① 12〜18 / body② 12〜21 / body③④ 12〜18 / comment 18〜25
                 上限+1〜2文字 → 軽微（トリミング）
                 上限+3文字以上・下限未満 → 重度
  構造         : body①「実は」始まり / body③「だから/それで/そのせいで」始まり
                 bodyが日本語を含む                → 重度
  NGワード     : 「いつも」「必ず」               → 重度
  重複         : body②③の名詞共通語 2語以上      → 重度
"""

import re
import sys
import time
import os
import configparser
from pathlib import Path
from dataclasses import dataclass, field

# log_writer は同じ scripts/ ディレクトリに置く
sys.path.insert(0, str(Path(__file__).parent))
from log_writer import write_genkou_check_log, write_process_log

# ===== パス設定 =====
BASE_DIR    = Path(__file__).resolve().parent.parent
LOG_DIR     = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config" / "genkou_checker_config.ini"

# ===== コンフィグ読み込み =====
def _load_cfg(config_path: Path = CONFIG_PATH) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if config_path.exists():
        cfg.read(str(config_path), encoding="utf-8")
    return cfg

_cfg = _load_cfg()

MAX_ATTEMPTS      = _cfg.getint("check",     "max_attempts",   fallback=3)
INTRO_MIN         = _cfg.getint("length",    "intro_min",      fallback=12)
INTRO_MAX         = _cfg.getint("length",    "intro_max",      fallback=20)
BODY_PART_MIN     = _cfg.getint("length",    "body_part_min",  fallback=12)
BODY_PART_MAX     = _cfg.getint("length",    "body_part_max",  fallback=18)
BODY_PART2_MAX    = _cfg.getint("length",    "body_part2_max", fallback=21)
COMMENT_MIN       = _cfg.getint("length",    "comment_min",    fallback=18)
COMMENT_MAX       = _cfg.getint("length",    "comment_max",    fallback=25)
MINOR_OVER        = _cfg.getint("length",    "minor_over",     fallback=2)
BODY1_PREFIX      = _cfg.get(   "structure", "body1_prefix",   fallback="実は")
BODY3_PREFIXES    = tuple(
    s.strip() for s in
    _cfg.get("structure", "body3_prefixes", fallback="だから,それで,そのせいで").split(",")
)
NG_WORDS          = [
    s.strip() for s in
    _cfg.get("ngwords", "words", fallback="いつも,必ず,絶対,全部,すべて,とても,すごく").split(",")
]


# ============================================================
#  前置き・後置き除去
# ============================================================
def strip_preamble(raw: str) -> tuple[str, str, bool]:
    """
    APIが出力した前置き・後置き・コードブロックを除去し、
    CSV行のみを返す。
    戻り値: (クリーン後テキスト, 除去された前置き文字列, コードブロックあったか)

    対応パターン:
      - 「わかりました」「生成します」「以下が台本です」などの前置き文
      - ```csv ... ``` などのコードブロック
      - 出力後の補足説明（後置き）
    """
    # ① コードブロックのフェンスを検出・除去
    had_codeblock = bool(re.search(r'```', raw))
    cleaned = re.sub(r'```[a-z]*\n?', '', raw).strip()

    # ② 行ごとに走査し「数字,」で始まる行をCSV行として抽出
    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]

    csv_line       = None
    preamble_lines = []
    found          = False

    for line in lines:
        if not found and re.match(r'^\d+,', line):
            csv_line = line
            found    = True
        elif not found:
            preamble_lines.append(line)
        # CSV行以降の後置きは無視

    if csv_line is None:
        return raw, "", had_codeblock  # CSV行が見つからない場合は元のまま返す

    preamble = "\n".join(preamble_lines)
    return csv_line, preamble, had_codeblock


# ============================================================
#  チェック結果データクラス
# ============================================================
@dataclass
class CheckResult:
    status: str = "ok"          # ok / fixed / rejected
    reject_reason: str = ""     # 重度エラーの理由
    errors: list = field(default_factory=list)   # 検出エラー一覧
    fixes:  list = field(default_factory=list)   # 適用修正一覧
    ng_words_hit: list = field(default_factory=list)  # ヒットしたNGワード
    preamble: str = ""          # 除去された前置き文字列
    intro_length: int   = 0
    body_lengths: list  = field(default_factory=list)
    comment_length: int = 0
    script: str = ""            # チェック後の台本文字列


# ============================================================
#  動詞語幹抽出（重複チェック用）
# ============================================================
def _extract_verb_stem(text: str) -> str:
    """
    文末の動詞を抽出し語幹を返す。
    活用語尾（る/た/て/ず/よ/ん/だ/ね 等）を除去して語幹にする。
    例）「集まるんだ」→「集ま」 / 「見えるよ」→「見え」
    """
    # 文末の語尾パターンを除去（長い順に適用）
    suffixes = [
        "るんだよ", "るんだ", "てるよ", "てるんだ", "るよ！", "るよ",
        "えるよ", "えるんだ", "くなる", "になる",
        "するよ", "するんだ", "してる",
        "んだよ", "んだね", "んだ", "だよ", "だね",
        "てる", "てた", "ている", "ていた",
        "るよ", "るね", "るぞ", "るか",
        "った", "って", "ってる",
        "ずに", "ない", "なく",
        "よ！", "ね！", "ぞ！",
        "よ", "ね", "ぞ", "か", "る", "た",
    ]
    stem = re.sub(r"[。、！？]+$", "", text)  # 末尾記号除去
    for suf in suffixes:
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    return stem



def _check_body_endings(body_parts: list) -> list[str]:
    """body各文の文末が「〜ん」で終わっていないかチェック。"""
    errors = []
    labels = ["①", "②", "③", "④"]
    for i, bp in enumerate(body_parts):
        # 末尾の記号を除去して確認
        stripped = bp.rstrip("！？。")
        if stripped.endswith("ん"):
            errors.append(f"ENDING:body{labels[i]}が「ん」で終わっている（→「んだ」に修正必要）")
    return errors


def _check_comment_format(comment: str) -> list[str]:
    """commentが「つまり〜ってこと！」形式かチェック。"""
    errors = []
    if not comment.startswith("つまり"):
        errors.append("FORMAT:commentが「つまり」で始まらない")
    if not comment.endswith("ってこと！"):
        errors.append("FORMAT:commentが「ってこと！」で終わらない")
    return errors


def _check_desu_masu(body: str) -> list[str]:
    """bodyにです・ます調が混入していないかチェック。"""
    if re.search(r'(です|ます|でした|ました)(?=[。！？よねよ|]|$)', body):
        return ["STYLE:bodyにです・ます調が混入している"]
    return []


# ============================================================
#  個別チェック関数
# ============================================================
def _check_format(parts: list) -> list[str]:
    """フォーマットチェック。エラーリストを返す。"""
    errors = []
    if len(parts) != 5:
        errors.append(f"FORMAT:カンマ数不正（{len(parts)-1}個）")
    return errors


def _check_body_split(body_parts: list) -> list[str]:
    errors = []
    if len(body_parts) != 4:
        errors.append(f"FORMAT:body区切り数不正（{len(body_parts)}個）")
    return errors


def _check_japanese(body: str) -> list[str]:
    errors = []
    if not re.search(r"[ぁ-んァ-ヶ一-龯]", body):
        errors.append("STRUCTURE:bodyに日本語が含まれない")
    return errors


def _check_ng_words(text: str, field_name: str) -> tuple[list[str], list[str]]:
    """エラーリストとヒットワードリストのタプルを返す。"""
    errors = []
    hit_words = []
    for w in NG_WORDS:
        if w in text:
            errors.append(f"NGWORD:{field_name}に「{w}」が含まれる")
            hit_words.append(w)
    return errors, hit_words


def _check_structure(body_parts: list) -> list[str]:
    errors = []
    if len(body_parts) >= 1 and not body_parts[0].startswith(BODY1_PREFIX):
        errors.append(f"STRUCTURE:body①が「{BODY1_PREFIX}」で始まらない")
    if len(body_parts) >= 3 and not body_parts[2].startswith(BODY3_PREFIXES):
        errors.append(f"STRUCTURE:body③が{'・'.join(BODY3_PREFIXES)}で始まらない")
    return errors


def _check_duplicate(body_parts: list) -> list[str]:
    """
    body②とbody③の重複チェック。以下のいずれかでNG（重度エラー）。

    ① 動詞チェック: ②と③の文末動詞語幹が一致する
    ② パターンチェック: ③から接続詞を除いた文字列が②に80%以上含まれる
    """
    errors = []
    if len(body_parts) < 3:
        return errors

    b2 = body_parts[1].strip()
    b3 = body_parts[2].strip()

    # ① 動詞語幹チェック
    stem2 = _extract_verb_stem(b2)
    stem3 = _extract_verb_stem(b3)
    # 語幹が2文字以上かつ「完全一致」または「③の語幹が②の語幹を含む」場合はNG
    if len(stem2) >= 2 and (stem2 == stem3 or stem2 in stem3):
        errors.append(f"DUPLICATE:body②③の動詞語幹が一致（語幹=「{stem2}」）")
        return errors

    # ② パターンチェック: ③から接続詞を除いた部分が②に含まれるか
    b3_stripped = b3
    for prefix in BODY3_PREFIXES:
        if b3_stripped.startswith(prefix):
            b3_stripped = b3_stripped[len(prefix):].strip()
            break
    # b3_stripped の文字が b2 に何割含まれるか（文字集合ベース）
    if len(b3_stripped) >= 4:
        chars3 = set(b3_stripped)
        chars2 = set(b2)
        overlap = len(chars3 & chars2) / len(chars3)
        if overlap >= 0.8:
            errors.append(
                f"DUPLICATE:body③がbody②の言い換えになっている"
                f"（類似度={overlap:.0%}）"
            )

    return errors


def _check_length(text: str, min_len: int, max_len: int,
                  field_name: str) -> tuple[list[str], list[str]]:
    """
    文字数チェック。
    戻り値: (minor_errors, major_errors)
    """
    minor, major = [], []
    length = len(text)
    if length < min_len:
        major.append(f"LENGTH:{field_name}が短すぎる（{length}文字、最低{min_len}文字）")
    elif length > max_len + MINOR_OVER:
        major.append(f"LENGTH:{field_name}が長すぎる（{length}文字、上限{max_len}+{MINOR_OVER}超）")
    elif length > max_len:
        minor.append(f"LENGTH:{field_name}が{length - max_len}文字オーバー（{length}文字）")
    return minor, major


# ============================================================
#  自動修正関数
# ============================================================
def _trim_to_length(text: str, max_len: int, field_name: str) -> tuple[str, str]:
    """
    文末から文字を削ってmax_len以内に収める。
    戻り値: (修正後テキスト, 修正内容の説明)
    """
    trimmed = text[:max_len]
    fix_desc = f"TRIM:{field_name}を{len(text)}→{len(trimmed)}文字にトリミング"
    return trimmed, fix_desc


# ============================================================
#  メインチェック関数
# ============================================================
def check_script(raw: str) -> CheckResult:
    """
    台本CSV文字列を受け取り、チェック・修正を行い CheckResult を返す。
    """
    result = CheckResult(script=raw)
    all_errors: list[str] = []
    major_errors: list[str] = []
    minor_errors: list[str] = []
    fixes: list[str] = []

    # ── 全角カンマ・句読点を半角に正規化 ──────────────────
    raw = raw.replace('，', ',').replace('　', ' ')

    # ── 前置き・後置き除去 ───────────────────────────────
    cleaned, preamble, had_codeblock = strip_preamble(raw)
    if preamble:
        result.preamble = preamble
        fixes.append(f"PREAMBLE:前置きを除去（「{preamble[:30]}{'...' if len(preamble)>30 else ''}」）")
    if had_codeblock:
        fixes.append("PREAMBLE:コードブロック（```）を除去")
    raw = cleaned

    # ── フォーマットチェック ───────────────────────────────
    parts = raw.split(",", maxsplit=4)
    fmt_errors = _check_format(parts)
    if fmt_errors:
        result.status        = "rejected"
        result.reject_reason = fmt_errors[0]
        result.errors        = fmt_errors
        return result

    _, name, intro, body, comment = [p.strip() for p in parts]
    body_parts = body.split("|")

    # body 分割チェック
    split_errors = _check_body_split(body_parts)
    major_errors.extend(split_errors)

    # 日本語チェック
    major_errors.extend(_check_japanese(body))

    # NGワードチェック
    ng_hits: list[str] = []
    for field_name, text in [("intro", intro), ("body", body), ("comment", comment)]:
        ng_errs, ng_words = _check_ng_words(text, field_name)
        major_errors.extend(ng_errs)
        ng_hits.extend(ng_words)

    # 構造チェック（body分割が正常な場合のみ）
    if not split_errors:
        major_errors.extend(_check_structure(body_parts))
        major_errors.extend(_check_duplicate(body_parts))
        major_errors.extend(_check_body_endings(body_parts))
        major_errors.extend(_check_desu_masu(body))

    # comment形式チェック
    major_errors.extend(_check_comment_format(comment))

    # ── 文字数チェック ────────────────────────────────────
    # intro
    mn, mj = _check_length(intro, INTRO_MIN, INTRO_MAX, "intro")
    minor_errors.extend(mn)
    major_errors.extend(mj)

    # body 各区切り（②のみ上限が異なる）
    for i, bp in enumerate(body_parts):
        max_len = BODY_PART2_MAX if i == 1 else BODY_PART_MAX
        mn, mj = _check_length(bp, BODY_PART_MIN, max_len, f"body{['①','②','③','④'][i]}")
        minor_errors.extend(mn)
        major_errors.extend(mj)

    # comment
    mn, mj = _check_length(comment, COMMENT_MIN, COMMENT_MAX, "comment")
    minor_errors.extend(mn)
    major_errors.extend(mj)

    all_errors = major_errors + minor_errors

    # ── 重度エラー → rejected ─────────────────────────────
    if major_errors:
        result.status        = "rejected"
        result.reject_reason = major_errors[0]
        result.errors        = all_errors
        result.ng_words_hit  = ng_hits
        result.intro_length  = len(intro)
        result.body_lengths  = [len(bp) for bp in body_parts]
        result.comment_length = len(comment)
        return result

    # ── 軽微エラー → 自動修正 ────────────────────────────
    if minor_errors:
        # intro トリミング
        if len(intro) > INTRO_MAX:
            intro, fix = _trim_to_length(intro, INTRO_MAX, "intro")
            fixes.append(fix)

        # body 各区切りトリミング（②のみ上限が異なる）
        new_body_parts = []
        trim_caused_n_ending = False
        for i, bp in enumerate(body_parts):
            max_len = BODY_PART2_MAX if i == 1 else BODY_PART_MAX
            if len(bp) > max_len:
                trimmed, fix = _trim_to_length(bp, max_len, f"body{['①','②','③','④'][i]}")
                # トリミング後に「ん」で終わる場合は重度エラーに昇格
                stripped = trimmed.rstrip("！？。")
                if stripped.endswith("ん"):
                    major_errors.append(
                        f"TRIM_ENDING:body{['①','②','③','④'][i]}のトリミング後が「ん」で終わる（再生成必要）"
                    )
                    trim_caused_n_ending = True
                else:
                    fixes.append(fix)
                    bp = trimmed
            new_body_parts.append(bp)

        if trim_caused_n_ending:
            result.status        = "rejected"
            result.reject_reason = major_errors[-1]
            result.errors        = major_errors
            result.intro_length  = len(intro)
            result.body_lengths  = [len(bp) for bp in body_parts]
            result.comment_length = len(comment)
            return result

        body_parts = new_body_parts
        body = "|".join(body_parts)

        # comment トリミング
        if len(comment) > COMMENT_MAX:
            comment, fix = _trim_to_length(comment, COMMENT_MAX, "comment")
            fixes.append(fix)

        # 修正後の台本を再組み立て
        result.script = f"{parts[0]},{name},{intro},{body},{comment}"
        result.status = "fixed"
        result.fixes  = fixes

    # ── 前置き除去のみの場合もfixedとして記録 ────────────
    if (preamble or had_codeblock) and result.status == "ok":
        result.status = "fixed"
        result.fixes  = fixes

    # ── 計測値を記録 ─────────────────────────────────────
    result.errors         = all_errors
    result.ng_words_hit   = ng_hits
    result.intro_length   = len(intro)
    result.body_lengths   = [len(bp) for bp in body_parts]
    result.comment_length = len(comment)
    if not result.fixes:
        result.fixes = fixes

    return result


# ============================================================
#  外部から呼び出すメインAPI
# ============================================================
def run_check(
    raw_script: str,
    entry_id: str,
    generate_func,          # 再生成用コールバック: () -> str
    log_dir: Path = LOG_DIR,
) -> str | None:
    """
    台本チェック→修正→再生成を最大MAX_ATTEMPTS回行い、
    採用できた台本CSV文字列を返す。
    すべて失敗した場合は None を返す。

    Args:
        raw_script:    最初の生成結果（CSV文字列）
        entry_id:      台本ID（ログ記録用）
        generate_func: 再生成を呼び出すコールバック関数
        log_dir:       ログ出力ディレクトリ
    """
    current_raw = raw_script

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = check_script(current_raw)

        if result.status in ("ok", "fixed"):
            # ── 採用 ──────────────────────────────────────
            write_genkou_check_log(
                log_dir,
                entry_id       = entry_id,
                attempt        = attempt,
                status         = result.status,
                errors         = result.errors,
                fixes          = result.fixes,
                intro_length   = result.intro_length,
                body_lengths   = result.body_lengths,
                comment_length = result.comment_length,
                raw_script     = current_raw,
                final_script   = result.script,
                ng_words_hit   = result.ng_words_hit,
            )
            write_process_log(
                log_dir, video_id=entry_id, process="genkou_check",
                status="success",
                title=f"attempt={attempt} status={result.status}",
            )
            return result.script

        # ── rejected → ログ記録して再生成 ────────────────
        write_genkou_check_log(
            log_dir,
            entry_id       = entry_id,
            attempt        = attempt,
            status         = "rejected",
            reject_reason  = result.reject_reason,
            errors         = result.errors,
            intro_length   = result.intro_length,
            body_lengths   = result.body_lengths,
            comment_length = result.comment_length,
            raw_script     = current_raw,
            final_script   = "",
            ng_words_hit   = result.ng_words_hit,
        )
        write_process_log(
            log_dir, video_id=entry_id, process="genkou_check",
            status="failed",
            title=f"attempt={attempt} rejected={result.reject_reason}",
        )

        if attempt < MAX_ATTEMPTS:
            try:
                current_raw = generate_func()
            except Exception as e:
                write_process_log(
                    log_dir, video_id=entry_id, process="genkou_regenerate",
                    status="failed", error_message=str(e),
                )
                break
        else:
            # 3回すべて失敗
            write_process_log(
                log_dir, video_id=entry_id, process="genkou_check",
                status="failed",
                title=f"全{MAX_ATTEMPTS}回失敗・手動確認対象",
                error_message=result.reject_reason,
            )

    return None  # すべて失敗 → 呼び出し元で保留処理


# ============================================================
#  単体テスト用
# ============================================================
if __name__ == "__main__":
    test_cases = [
        # (説明, 入力CSV)
        ("正常",
         "1,なんで空は青いの？,どうして空って青く見えるの！？,"
         "実は【太陽の光】から粒が来るよ|見えない小さな粒が飛ぶんだ|だから空気にぶつかると光るんだよ|晴れた日に青く見えることがあるよ,"
         "つまり空は青い光しか入れないひみつ基地ってこと！"),
        ("NGワード（いつも）",
         "1,なんで空は青いの？,どうして空って青く見えるの！？,"
         "実は光がある|光はいつも散らばるよ|だから青くなる|いつも青いんだ,"
         "つまり空は青いってこと！"),
        ("フォーマット不正",
         "1,なんで空は青いの？,intro only"),
        ("軽微エラー（intro 21文字）",
         "1,なんで空は青いの？,どうして空って青く光って見えるの！？,"
         "実は【太陽の光】から粒が来るよ|見えない小さな粒が飛ぶんだ|だから空気にぶつかると光るんだよ|晴れた日に青く見えることがあるよ,"
         "つまり空は青い光しか入れないひみつ基地ってこと！"),
        ("重複（body②③共通名詞2語以上）",
         "1,なんで空は青いの？,どうして空って青く見えるの！？,"
         "実は太陽の光が散らばるよ|太陽の光が空気に当たるよ|だから太陽の光が空気で広がるよ|晴れの日に見えるよ,"
         "つまり空は光の遊び場ってこと！"),
    ]

    import logging
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 55)
    print("  genkou_checker 単体テスト")
    print("=" * 55)
    for label, raw in test_cases:
        result = check_script(raw)
        print(f"\n[{label}]")
        print(f"  status        : {result.status}")
        print(f"  reject_reason : {result.reject_reason}")
        print(f"  errors        : {result.errors}")
        print(f"  fixes         : {result.fixes}")
        print(f"  intro_length  : {result.intro_length}")
        print(f"  body_lengths  : {result.body_lengths}")
        print(f"  comment_length: {result.comment_length}")
        if result.fixes:
            print(f"  fixed_script  : {result.script}")
