#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
なぜラボ Wiki Shorts 動画生成システム
genkou.csv (id, name, intro, body, comment, image_prompt) から
1エントリ1本のショート動画を生成する。

動画構成:
  [intro]   読み上げ + ポップイン (zoom 90pct to 100pct, 0.25s)
  [body]    読み上げ + 通常字幕
  [comment] 読み上げ + zoom110pct + 集中線オーバーレイ
各セクション末尾に0.4秒の無音

【themes.csv status体系】
  0: 初期（theme_generator で作成）
  1: 原稿生成済み → genkou_generater.py で status=0→1 に更新
  2: 画像生成済み → image_generator.py で status=1→2 に更新
  3: 動画生成済み → movie_generater.py で status=1→3 に更新（本スクリプト、status=2でも処理可）
  4: 投稿済み → youtube_uploader.py で status=3→4 に更新
  91: 原稿生成失敗スキップ ← genkou_generater.py でエラー時に status=91 を設定
  92: 画像生成失敗スキップ ← image_generator.py でリトライ超過時に status=92 を設定
  93: 動画生成失敗スキップ ← movie_generater.py でエラー時に status=93 を設定（本スクリプト）

【本スクリプトの処理フロー】
  status=1 または status=2 のエントリを処理：
    1. 必要なアセット（背景画像、BGMなど）の確認
    2. VoiceVox で音声生成
    3. FFmpeg で動画合成
    4. output/result/ に最終動画を保存
    - 成功 → status=3 に更新
    - 失敗 → status=93 に更新して失敗スキップ
"""

import csv
import math
import random
import re
import shutil
import subprocess
import sys
import time as _time
import wave
import argparse
import platform
from pathlib import Path
from dataclasses import dataclass

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# scripts/ 配下のモジュールをインポート
sys.path.insert(0, str(Path(__file__).parent))
from path_helper import (
    get_channel_data_dir,
    get_channel_config_dir,
    get_channel_output_dir,
    get_channel_logs_dir,
)
from log_writer import write_movie_gen_log, write_process_log

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

try:
    import requests
except ImportError:
    requests = None

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ========================================
# パス定数
# ========================================

BASE_DIR   = Path(__file__).parent.parent
DATA_DIR   = BASE_DIR / "data"
BG_DIR     = DATA_DIR / "background"
BGM_DIR    = DATA_DIR / "bgm"
SOUND_DIR  = DATA_DIR / "sound"
CHARA_DIR  = DATA_DIR / "characters"
OUTPUT_DIR = BASE_DIR / "output"
RESULT_DIR = OUTPUT_DIR / "result"
WORK_DIR   = OUTPUT_DIR / "work"
LOG_DIR    = BASE_DIR / "logs"

GENKOU_CSV_PATH       = DATA_DIR / "genkou.csv"
THEMES_CSV_PATH       = BASE_DIR / "scripts" / "themes.csv"
CONFIG_PATH           = BASE_DIR / "config" / "move_gen.toml"
SEQUENTIAL_STATE_FILE = OUTPUT_DIR / "wiki_seq_state.txt"
INTRO_VOICE_PATH      = SOUND_DIR / "intro_voice.wav"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
BGM_EXTENSIONS   = {".mp3", ".wav", ".ogg", ".aac", ".m4a"}

VOICEVOX_URL = "http://127.0.0.1:50021"
SILENCE_SEC  = 0.4

# ============================================================
# YouTube Shorts レイアウト定数
# ============================================================
SUBTITLE_Y1  = 0      # 字幕ゾーン上端
SUBTITLE_Y2  = 420    # 字幕ゾーン下端
CHARA_Y1     = 420    # キャラゾーン上端
CHARA_Y2     = 1500   # キャラゾーン下端
UI_Y1        = 1500   # UIセーフマージン上端（コンテンツ配置禁止）
MAX_SUB_W    = 864    # 字幕最大幅

# ========================================
# データクラス
# ========================================

@dataclass
class WikiEntry:
    id:           str
    name:         str
    intro:        str
    body:         str
    comment:      str
    themes:       str = ""   # themes.csv の themes 列（背景・キャラフォルダ名に使用）
    image_prompt: str = ""

    @classmethod
    def from_dict(cls, row: dict) -> "WikiEntry":
        def col(key):
            v = row.get(key) or ""
            return v.strip().replace("\\n", "\n")
        return cls(
            id           = col("id"),
            name         = col("name"),
            intro        = col("intro"),
            body         = col("body"),
            comment      = col("comment"),
            themes       = col("themes"),
            image_prompt = col("image_prompt"),
        )

# ========================================
# 設定読み込み
# ========================================

def load_config(path: Path = CONFIG_PATH) -> dict:
    defaults = {
        "video":    {"count": 1, "width": 1080, "height": 1920, "fps": 30},
        "voicevox": {"speaker": 1, "volume": 1.0, "speed": 1.4,
                     "pitch": 0.0, "intonation": 1.2},
        "bgm":      {"volume": 0.2},
        "subtitle": {
            "font_size": 84, "font_path": "",
            "primary_colour": "#FFFFFF", "outline_colour": "#000000",
            "quote_colour": "#FFD54F",
            "outline": 3, "shadow": 0, "bold": 1,
            "alignment": 2,
            "margin_bottom": 420, "margin_top": 230, "margin_lr": 108,
            "line_spacing": 17,
        },
        "subtitle_table": {
            "enabled": True, "color": "#2F80ED", "alpha": 230,
            "padding": 40, "padding_h": 60, "padding_v": 40,
            "radius": 40, "box_width": 864,
        },
        "background": {"color_top": [10, 20, 50], "color_bottom": [30, 60, 120], "blur": 0},
        "encode":    {"gpu": True, "codec": "h264", "quality": 23},
        "effects": {
            "intro_zoom_start": 0.90,
            "intro_zoom_end":   1.00,
            "intro_zoom_dur":   0.25,
            "comment_zoom":     1.10,
            "focus_lines":      24,
            "focus_alpha":      120,
            "focus_color":      "#FFFFFF",
            "focus_width":      3,
        },
        "sound": {
            "title_volume":          1.0,
            "important_word_volume": 1.0,
            "comment_volume":        1.0,
        },
    }
    if not path.exists():
        print(f"[設定] {path.name} が見つかりません。デフォルト値で続行します。")
        return defaults
    if tomllib is None:
        print("[警告] TOMLパーサー未インストール (pip install tomli)。デフォルト値で続行します。")
        return defaults
    with open(path, "rb") as f:
        user = tomllib.load(f)
    for k, v in user.items():
        if k in defaults and isinstance(v, dict):
            defaults[k].update(v)
        else:
            defaults[k] = v
    print(f"[設定] {path.name} を読み込みました")
    return defaults

# ========================================
# CSV読み込み
# ========================================

def load_genkou(csv_path: Path) -> list:
    REQUIRED = {"id", "name", "intro", "body", "comment"}
    entries = []
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"[CSVエラー] {csv_path.name}: ヘッダー行が読み取れません")
        actual = {k.strip() for k in reader.fieldnames}
        missing = REQUIRED - actual
        if missing:
            raise ValueError(
                f"[CSVエラー] {csv_path.name}: 必須列が見つかりません -> {sorted(missing)}\n"
                f"  検出された列: {sorted(actual)}"
            )
        for row_num, row in enumerate(reader, start=2):
            try:
                entries.append(WikiEntry.from_dict(row))
            except Exception as e:
                raise ValueError(f"[CSVエラー] {csv_path.name} {row_num}行目: {e}") from e
    entries.sort(key=lambda e: e.id)
    print(f"[データ] {len(entries)}件のエントリを読み込みました")
    return entries

# ========================================
# sequential状態管理
# ========================================

def update_themes_status(name: str, new_status: int) -> None:
    """
    themes.csv の name 列が一致する行の status を new_status に更新する。
    ファイルが存在しない・列がない場合はスキップ（エラーにしない）。
    """
    # scripts/themes.csv → data/themes.csv の順で探す
    csv_path = THEMES_CSV_PATH if THEMES_CSV_PATH.exists() else DATA_DIR / "themes.csv"
    if not csv_path.exists():
        return
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return
            fieldnames = list(reader.fieldnames)
            rows = list(reader)
        if "name" not in fieldnames or "status" not in fieldnames:
            return
        updated = False
        for row in rows:
            if row.get("name", "").strip() == name.strip():
                row["status"] = str(new_status)
                updated = True
        if updated:
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            print(f'[themes] "{name}" -> status={new_status}')
        else:
            print(f'[themes] "{name}" が themes.csv に見つかりません（スキップ）')
    except Exception as e:
        print(f"[themes] 更新スキップ: {e}")


def _load_pending_themes() -> set:
    """themes.csv から status==1（原稿生成済み）または status==2（背景生成済み）の name 一覧を返す"""
    # パス候補: scripts/themes.csv → data/themes.csv の順で探す
    candidates = [
        THEMES_CSV_PATH,
        DATA_DIR / "themes.csv",
    ]
    csv_path = None
    for p in candidates:
        if p.exists():
            csv_path = p
            break

    if csv_path is None:
        print(f"[themes] themes.csv が見つかりません")
        print(f"  探したパス:")
        for p in candidates:
            print(f"    {p}")
        return set()

    print(f"[themes] {csv_path}")
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return set()
            rows = list(reader)
        pending = {
            row["name"].strip()
            for row in rows
            if row.get("status", "").strip() in ("1", "2")   # 原稿生成済み or 背景生成済み
        }
        all_statuses = [row.get("status", "").strip() for row in rows]
        from collections import Counter
        print(f"[themes] 全{len(rows)}件 / status分布: {dict(Counter(all_statuses))}")
        return pending
    except Exception as e:
        print(f"[themes] 読み込みエラー: {e}")
        return set()


def _load_seq_index() -> int:
    try:
        return int(SEQUENTIAL_STATE_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0

def _save_seq_index(idx: int) -> None:
    SEQUENTIAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEQUENTIAL_STATE_FILE.write_text(str(idx), encoding="utf-8")

# ========================================
# ユーティリティ
# ========================================

def _safe_filename(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r\t]', "_", s).strip("_ ")

def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#").zfill(6)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _hex_to_rgba(h: str, alpha: int = 180) -> tuple:
    r, g, b = _hex_to_rgb(h)
    return (r, g, b, alpha)

# ========================================
# フォント検索
# ========================================

_font_path_cache = None

def _find_japanese_font(hint: str = "") -> str:
    if hint:
        p = Path(hint)
        if p.exists():
            return str(p)

    def win_font_dirs():
        import os
        dirs = [Path("C:/Windows/Fonts")]
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return dirs

    win_candidates = [
        "NotoSansJP-Black.otf", "NotoSansJP-Bold.otf",
        "NotoSerifJP-Bold.otf", "meiryo.ttc", "YuGothM.ttc", "msgothic.ttc",
    ]
    linux_candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    ]

    if hint:
        name = Path(hint).name
        if platform.system() == "Windows":
            for d in win_font_dirs():
                for m in d.rglob(name):
                    print(f"[字幕] フォント検出: {m}")
                    return str(m)

    if platform.system() == "Windows":
        for d in win_font_dirs():
            for name in win_candidates:
                ms = list(d.rglob(name))
                if ms:
                    print(f"[字幕] フォント検出: {ms[0]}")
                    return str(ms[0])
    else:
        for p in linux_candidates:
            if Path(p).exists():
                return p

    print("[警告] 日本語フォントが見つかりません。wiki_config.toml の subtitle.font_path を設定してください。")
    return ""

def _get_font_path(cfg: dict) -> str:
    global _font_path_cache
    if _font_path_cache is None:
        _font_path_cache = _find_japanese_font(cfg["subtitle"].get("font_path", ""))
    return _font_path_cache

# ========================================
# テキスト折り返し
# ========================================

def _wrap_text(text: str, max_zenkaku: int) -> str:
    KINSOKU_HEAD = set('。、．，！？」』）】〕〉》｝ー…‥・')
    KINSOKU_TAIL = set('「『（【〔〈《｛')
    # この文字の直後で改行しやすい（助詞・読点・文末語）
    BREAK_AFTER  = set('。、，はがをにでもとのからまでよだね')

    max_w = max_zenkaku * 2

    def char_w(ch): return 2 if ord(ch) > 0x7F else 1
    def text_w(s):  return sum(char_w(c) for c in s)

    def try_wrap(t: str) -> list:
        if text_w(t) <= max_w:
            return [t]
        total = text_w(t)
        half  = total // 2

        # 中央インデックスを求める
        acc = 0
        center_i = len(t)
        for i, c in enumerate(t):
            acc += char_w(c)
            if acc >= half:
                center_i = i + 1
                break

        # ① 中央付近でBREAK_AFTER優先の分割点を探す（中央に近い順）
        search_range = range(max(1, center_i - 4), min(len(t), center_i + 5))
        candidates = sorted(search_range, key=lambda i: abs(i - center_i))
        for si in candidates:
            if 0 < si < len(t):
                if t[si - 1] in BREAK_AFTER:
                    if t[si] not in KINSOKU_HEAD and t[si - 1] not in KINSOKU_TAIL:
                        return [t[:si], t[si:]]

        # ② BREAK_AFTERが見つからなければ禁則処理のみで分割
        for delta in range(0, min(5, len(t))):
            for si in [center_i + delta, center_i - delta]:
                if 0 < si < len(t):
                    if t[si] not in KINSOKU_HEAD and t[si - 1] not in KINSOKU_TAIL:
                        return [t[:si], t[si:]]

        return [t[:center_i], t[center_i:]]

    result = []
    for seg in text.split("\n"):
        result.extend(try_wrap(seg))
    return "\n".join(result)

# ========================================
# 音声生成
# ========================================

def check_voicevox() -> bool:
    try:
        return requests.get(f"{VOICEVOX_URL}/version", timeout=3).status_code == 200
    except Exception:
        return False

def check_ffmpeg() -> bool:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False

def generate_audio_voicevox(text: str, output_path: Path, cfg: dict) -> float:
    v = cfg["voicevox"]
    q = requests.post(f"{VOICEVOX_URL}/audio_query",
                      params={"text": text, "speaker": v["speaker"]}, timeout=10)
    q.raise_for_status()
    query = q.json()
    query["volumeScale"]     = v["volume"]
    query["speedScale"]      = v["speed"]
    query["pitchScale"]      = v["pitch"]
    query["intonationScale"] = v["intonation"]
    s = requests.post(f"{VOICEVOX_URL}/synthesis",
                      params={"speaker": v["speaker"]}, json=query, timeout=30)
    s.raise_for_status()
    output_path.write_bytes(s.content)
    return get_wav_duration(output_path)

def get_wav_duration(wav_path: Path) -> float:
    """WAVファイルの正確な長さを wave モジュールで取得"""
    with wave.open(str(wav_path), "r") as wf:
        return wf.getnframes() / wf.getframerate()

def get_audio_duration_ffprobe(audio_path: Path) -> float:
    """ffprobeで音声ファイルの正確な長さを取得（mp3/wavどちらも対応）"""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return get_wav_duration(audio_path)

def append_silence(wav_path: Path, silence_sec: float) -> float:
    with wave.open(str(wav_path), "r") as wf:
        params = wf.getparams()
        frames = wf.readframes(wf.getnframes())
    silent = b"\x00" * int(params.framerate * params.nchannels * params.sampwidth * silence_sec)
    with wave.open(str(wav_path), "w") as wf:
        wf.setparams(params)
        wf.writeframes(frames + silent)
    return get_wav_duration(wav_path)

def generate_dummy_audio(text: str, output_path: Path) -> float:
    duration = max(1.0, len(text) * 0.1)
    with wave.open(str(output_path), "w") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24000)
        wf.writeframes(b"\x00\x00" * int(24000 * duration))
    return duration

def gen_audio(text: str, path: Path, use_voicevox: bool, cfg: dict) -> float:
    print(f"[音声] 生成中: {text[:40]}...")
    return generate_audio_voicevox(text, path, cfg) if use_voicevox \
           else generate_dummy_audio(text, path)

# ========================================
# エンコード引数
# ========================================

def _video_encode_args(cfg: dict) -> list:
    ec      = cfg.get("encode", {})
    use_gpu = ec.get("gpu", False)
    codec   = ec.get("codec", "h264")
    quality = ec.get("quality", 23)
    if use_gpu:
        vcodec = "hevc_nvenc" if codec == "h265" else "h264_nvenc"
        return ["-c:v", vcodec, "-rc", "vbr", "-cq", str(quality), "-preset", "p4", "-b:v", "0"]
    else:
        vcodec = "libx265" if codec == "h265" else "libx264"
        return ["-c:v", vcodec, "-preset", "fast", "-crf", str(quality)]

# ========================================
# 背景・BGM検索
# ========================================

def find_bg_for_id(entry_id: str) -> Path:
    """idと同名（例: "001" -> 001.png）の背景画像を返す"""
    if BG_DIR.exists():
        for p in BG_DIR.iterdir():
            if p.suffix.lower() in IMAGE_EXTENSIONS and p.stem == entry_id:
                return p
    return None

def find_bgm() -> Path:
    if not BGM_DIR.exists():
        return None
    candidates = [p for p in BGM_DIR.iterdir() if p.suffix.lower() in BGM_EXTENSIONS]
    if not candidates:
        return None
    chosen = candidates[0]
    print(f"[BGM] 素材を使用: {chosen.name}")
    return chosen

def find_sound_effects() -> dict:
    """
    data/sound/ 以下のサブディレクトリから効果音ファイルを1つずつ取得する。
    戻り値: {"title": Path|None, "important_word": Path|None, "comment": Path|None}
    ファイルが存在しないキーは None。
    """
    keys = {
        "title":          "sound_title",
        "important_word": "sound_important_word",
        "comment":        "sound_comment",
    }
    result = {}
    for key, dirname in keys.items():
        d = SOUND_DIR / dirname
        if d.exists():
            # intro_voice.wav は別用途のため除外
            files = sorted(
                p for p in d.iterdir()
                if p.suffix.lower() in BGM_EXTENSIONS
                and p.name != INTRO_VOICE_PATH.name
            )
            result[key] = files[0] if files else None
        else:
            result[key] = None

    for key, path in result.items():
        if path:
            print(f"[効果音] {key}: {path.name}")
        else:
            print(f"[効果音] {key}: なし（スキップ）")
    return result

def _create_background(output_path: Path, cfg: dict) -> None:
    vw = cfg["video"]["width"]
    vh = cfg["video"]["height"]
    ct = tuple(cfg["background"]["color_top"])
    cb = tuple(cfg["background"]["color_bottom"])
    img  = Image.new("RGB", (vw, vh))
    draw = ImageDraw.Draw(img)
    for y in range(vh):
        r = y / vh
        draw.line([(0, y), (vw, y)], fill=(
            int(ct[0] + (cb[0]-ct[0])*r),
            int(ct[1] + (cb[1]-ct[1])*r),
            int(ct[2] + (cb[2]-ct[2])*r),
        ))
    img.save(str(output_path))
    print(f"[背景] グラデーション自動生成: {output_path.name}")

# ========================================
# 集中線描画
# ========================================

# ========================================
# オーバーレイ生成 (Pillow) ― 背景を触らない透過PNG
# ========================================

def _split_segments(text: str) -> list:
    """【...】で囲まれた部分を強調（quote）セグメントとして分割する。"""
    parts = re.split(r'(【.*?】)', text)
    return [(p, p.startswith('【') and p.endswith('】') and len(p) >= 3) for p in parts if p]

def _make_subtitle_overlay(
    out_path:     Path,
    text:         str,
    cfg:          dict,
    subtitle_key: str = "subtitle",
) -> tuple:
    """
    字幕（テーブル背景＋テキスト）を透過PNGとして out_path に保存する。
    subtitle_key: cfg から読む字幕設定キー。セクション別スタイルに対応。
    存在しないキーは "subtitle" にフォールバック。
    戻り値: (table_cx, table_cy)
    """
    sc           = cfg.get(subtitle_key, cfg["subtitle"])
    fontsize     = sc["font_size"]
    align        = sc["alignment"]
    margin_v     = sc["margin_bottom"]
    margin_t     = sc.get("margin_top", margin_v)
    margin_h     = sc["margin_lr"]
    vw           = cfg["video"]["width"]
    vh           = cfg["video"]["height"]
    outline      = sc["outline"]
    line_spacing = sc.get("line_spacing", max(4, int(fontsize * 0.2)))

    table_key       = subtitle_key + "_table"
    tb              = cfg.get(table_key, cfg.get("subtitle_table", {}))
    table_enabled   = tb.get("enabled", False)
    table_padding_h = tb.get("padding_h", tb.get("padding", 12))
    table_padding_v = tb.get("padding_v", tb.get("padding", 12))
    box_width       = tb.get("box_width", 0)
    table_color     = _hex_to_rgba(tb.get("color", "#000000"), tb.get("alpha", 180))
    table_radius    = tb.get("radius", 8)

    fc     = _hex_to_rgb(sc["primary_colour"])
    bc     = _hex_to_rgb(sc["outline_colour"])
    qc_hex = sc.get("quote_colour", "").strip()
    qc     = _hex_to_rgb(qc_hex) if qc_hex else fc

    font_path = _get_font_path(cfg)
    try:
        font = ImageFont.truetype(font_path, fontsize) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    # 完全透明なキャンバス（背景画像不要）
    canvas = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
    draw   = ImageDraw.Draw(canvas)

    # テキスト折り返し
    if box_width > 0:
        inner_w = box_width - table_padding_h * 2
    else:
        inner_w = vw - margin_h * 2
    # 実際のフォント幅ベースで折り返し（セグメント情報を保持）
    # lines_segs: [[(seg_text, is_quote), ...], ...] 行ごとのセグメントリスト
    _wrap_canvas = Image.new("RGBA", (inner_w * 3, fontsize * 2), (0,0,0,0))
    _wrap_draw   = ImageDraw.Draw(_wrap_canvas)
    lines_segs = []  # 行ごとの [(text, is_quote), ...]
    lines      = []  # 行ごとの文字列（textbbox計算用）
    # 行頭禁則文字（ぶら下げ処理対象）
    _KINSOKU_HEAD = set('。、．，！？」』）】〕〉》｝ー…‥・')
    for para in text.split("\n"):
        # セグメント分割してから折り返す（quote情報を保持）
        para_segs = _split_segments(para)
        cur_line_segs: list = []  # 現在行のセグメント
        cur_line_w   = 0          # 現在行の幅
        for seg_text, is_q in para_segs:
            seg_buf = ""           # セグメント内の累積文字列
            seg_buf_q = is_q
            for ch in seg_text:
                ch_w = _wrap_draw.textbbox((0, 0), ch, font=font)[2]
                if cur_line_w + ch_w > inner_w and (cur_line_segs or seg_buf):
                    if ch in _KINSOKU_HEAD:
                        # 行頭禁則（ぶら下げ）: 現在行末に押し込んでから改行
                        seg_buf    += ch
                        cur_line_w += ch_w
                        if seg_buf:
                            cur_line_segs.append((seg_buf, seg_buf_q))
                            seg_buf = ""
                        lines_segs.append(cur_line_segs)
                        lines.append("".join(s for s, _ in cur_line_segs))
                        cur_line_segs = []
                        cur_line_w    = 0
                        continue
                    # 通常折り返し
                    if seg_buf:
                        cur_line_segs.append((seg_buf, seg_buf_q))
                        seg_buf = ""
                    line_text = "".join(s for s, _ in cur_line_segs)
                    lines_segs.append(cur_line_segs)
                    lines.append(line_text)
                    cur_line_segs = []
                    cur_line_w    = 0
                seg_buf    += ch
                cur_line_w += ch_w
            if seg_buf:
                cur_line_segs.append((seg_buf, seg_buf_q))
        if cur_line_segs:
            line_text = "".join(s for s, _ in cur_line_segs)
            lines_segs.append(cur_line_segs)
            lines.append(line_text)

    table_cx, table_cy = vw // 2, vh // 2

    if not lines:
        canvas.save(str(out_path))
        return table_cx, table_cy

    line_bboxes  = [draw.textbbox((0, 0), l, font=font) for l in lines]
    line_tops    = [bb[1]         for bb in line_bboxes]
    line_heights = [bb[3] - bb[1] for bb in line_bboxes]
    line_widths  = [bb[2] - bb[0] for bb in line_bboxes]
    total_h      = sum(line_heights) + line_spacing * (len(lines) - 1)

    col = (align - 1) % 3
    box_h = total_h + table_padding_v * 2 if table_enabled else total_h
    if align == 5:
        # alignment=5（正中央）: 字幕ゾーンを無視して画面全体の中央に配置
        by = (vh - box_h) // 2
        by = by + table_padding_v if table_enabled else by
    else:
        # margin_top = テーブル上端Y座標（px）
        # 内部で vh 比率に変換して保持し、最終的にピクセルに戻す
        # → 将来の解像度変更時は margin_top の px 値を変えるだけで追従可能
        margin_top_px    = sc.get("margin_top", SUBTITLE_Y1)
        margin_top_ratio = margin_top_px / vh          # 内部保持（比率）
        table_top        = int(margin_top_ratio * vh)  # テーブル上端Y（px）
        by = table_top + table_padding_v if table_enabled else table_top

    # box_width が設定されている場合、テキストをそのボックス内に収める
    if box_width > 0:
        box_x1 = vw // 2 - box_width // 2 + table_padding_h
        box_x2 = vw // 2 + box_width // 2 - table_padding_h
    else:
        box_x1 = margin_h
        box_x2 = vw - margin_h

    line_lx = []
    for lw in line_widths:
        lx = margin_h          if col == 0 else \
             (vw - lw) // 2    if col == 1 else vw - lw - margin_h
        # box_width 内に収まるようにクランプ
        if box_width > 0:
            lx = max(box_x1, min(lx, box_x2 - lw))
        line_lx.append(lx)

    line_ly = []
    y = by
    for lt, lh in zip(line_tops, line_heights):
        line_ly.append(y - lt)
        y += lh + line_spacing

    # テーブル背景
    if table_enabled and lines:
        actual_y1 = min(ly + lt for ly, lt in zip(line_ly, line_tops))
        actual_y2 = max(ly + lt + lh for ly, lt, lh in zip(line_ly, line_tops, line_heights))
        if box_width > 0:
            tx1 = vw // 2 - box_width // 2
            tx2 = vw // 2 + box_width // 2
        else:
            tx1 = min(line_lx) - table_padding_h
            tx2 = max(lx + lw for lx, lw in zip(line_lx, line_widths)) + table_padding_h
        ty1 = actual_y1 - table_padding_v
        ty2 = actual_y2 + table_padding_v
        table_cx = (tx1 + tx2) // 2
        table_cy = (ty1 + ty2) // 2

        if table_radius > 0:
            draw.rounded_rectangle([tx1, ty1, tx2, ty2], radius=table_radius, fill=table_color)
        else:
            draw.rectangle([tx1, ty1, tx2, ty2], fill=table_color)

    # テキスト描画（アウトライン → 本文）
    # 描画X を box 内に収める（左右はみ出し防止）
    if box_width > 0:
        draw_x1 = vw // 2 - box_width // 2 + table_padding_h + outline
        draw_x2 = vw // 2 + box_width // 2 - table_padding_h - outline
    else:
        draw_x1 = margin_h + outline
        draw_x2 = vw - margin_h - outline

    for line_seg_list, lx, ly, lt in zip(lines_segs, line_lx, line_ly, line_tops):
        # アウトライン描画
        x = max(lx, draw_x1)
        for seg_text, _ in line_seg_list:
            for dx in range(-outline, outline + 1):
                for dy in range(-outline, outline + 1):
                    if dx == 0 and dy == 0:
                        continue
                    draw.text((x + dx, ly + dy), seg_text, font=font, fill=bc + (255,))
            x += draw.textbbox((0, 0), seg_text, font=font)[2]
        # 本文描画
        x = max(lx, draw_x1)
        for seg_text, is_quote in line_seg_list:
            draw.text((x, ly), seg_text, font=font,
                      fill=(qc if is_quote else fc) + (255,))
            x += draw.textbbox((0, 0), seg_text, font=font)[2]

    canvas.save(str(out_path))
    return table_cx, table_cy


def _crop_bg_image(bg_path: Path, vw: int, vh: int, crop_x: int, crop_y: int) -> "Image.Image":
    """
    背景画像を (crop_x, crop_y) から vw×vh で切り出して返す。
    背景が vw×vh より小さい場合はリサイズ。
    """
    bg = Image.open(bg_path).convert("RGBA")
    bw, bh = bg.size
    if bw > vw or bh > vh:
        ox = min(crop_x, bw - vw)
        oy = min(crop_y, bh - vh)
        return bg.crop((ox, oy, ox + vw, oy + vh))
    return bg.resize((vw, vh), Image.LANCZOS)


def _composite_bg_overlay(
    bg_path:     Path,
    overlay_path: Path,
    out_path:    Path,
    vw: int,
    vh: int,
) -> None:
    """
    bg_path（RGB/RGBA画像）の上に overlay_path（RGBA透過PNG）をPillowでアルファ合成し
    out_path にRGBのPNGとして保存する。
    ffmpegのoverlay alpha問題を回避するためPillow側で合成する。
    bg_path はすでに vw×vh にトリムされた画像を想定する。
    """
    bg = Image.open(bg_path).convert("RGBA").resize((vw, vh), Image.LANCZOS)
    ov = Image.open(overlay_path).convert("RGBA")
    if ov.size != (vw, vh):
        ov = ov.resize((vw, vh), Image.LANCZOS)
    composite = Image.alpha_composite(bg, ov)
    composite.convert("RGB").save(str(out_path), "PNG")


def _find_chara_image(clip_type: str, page_idx: int = 0, themes_dir: str = "") -> Path | None:
    """
    clip_type と page_idx に対応するキャラ画像を data/characters/ から取得。

    ファイル名ルール:
      intro   → intro.png
      jingle  → call.png
      body    → page 0,1 = body_01.png / page 2以降 = body_02.png
      comment → comment.png
    """
    if clip_type == "intro":
        stems = ["intro"]
    elif clip_type == "jingle":
        stems = ["call"]
    elif clip_type == "body":
        stems = ["body_01"] if page_idx < 2 else ["body_02"]
    elif clip_type == "comment":
        stems = ["comment"]
    else:
        return None  # 未知の clip_type はキャラなし

    # themes サブフォルダを優先、なければ共通フォルダ
    search_dirs = []
    if themes_dir:
        sub = CHARA_DIR / themes_dir
        if sub.exists():
            search_dirs.append(sub)
    if CHARA_DIR.exists():
        search_dirs.append(CHARA_DIR)

    for d in search_dirs:
        for stem in stems:
            for ext in [".png", ".webp", ".jpg", ".jpeg"]:
                p = d / f"{stem}{ext}"
                if p.exists():
                    return p
    return None


def _composite_chara(
    base_path:  Path,
    clip_type:  str,
    cfg:        dict,
    out_path:   Path,
    vw: int,
    vh: int,
    page_idx:   int = 0,
    themes_dir: str = "",
) -> None:
    """
    base_path（RGB/RGBA PNG）にキャラ画像を合成して out_path に保存。
    キャラ画像がない場合は base_path をそのまま out_path にコピー。

    配置ルール（[layout] セクション）:
    - 水平中央
    - キャラゾーン（chara_zone_top〜chara_zone_bottom）内で下端基準配置
    - スケールは [character] の scale_<clip_type> を使用
    """
    chara_path = _find_chara_image(clip_type, page_idx, themes_dir)
    if chara_path is None:
        import shutil
        if str(base_path) != str(out_path):
            shutil.copy2(str(base_path), str(out_path))
        return

    layout = cfg.get("layout", {})
    chara_top    = layout.get("chara_zone_top",    CHARA_Y1)
    chara_bottom = layout.get("chara_zone_bottom",  CHARA_Y2)

    char_cfg = cfg.get("character", {})
    scale_key = f"scale_{clip_type}"
    scale = char_cfg.get(scale_key, char_cfg.get("scale", 0.5))

    chara = Image.open(chara_path).convert("RGBA")
    cw, ch = chara.size

    # スケール適用（ゾーン高さを超えないようクランプ）
    zone_h = chara_bottom - chara_top
    new_h  = int(ch * scale)
    new_h  = min(new_h, zone_h)
    new_w  = int(cw * (new_h / ch))
    chara  = chara.resize((new_w, new_h), Image.LANCZOS)

    # 水平中央・ゾーン下端基準
    paste_x = (vw - new_w) // 2
    paste_y = chara_bottom - new_h

    # 背景を必ず (vw, vh) にリサイズしてからキャラを合成
    # （任意サイズの背景画像でもキャラ座標が正しくなる）
    base = Image.open(base_path).convert("RGBA").resize((vw, vh), Image.LANCZOS)
    base.paste(chara, (paste_x, paste_y), chara)
    base.convert("RGB").save(str(out_path), "PNG")


def _get_chara_overlay_params(
    clip_type: str,
    cfg:       dict,
    vw: int,
    vh: int,
    page_idx:  int = 0,
    themes_dir: str = "",
) -> tuple:
    """
    キャラ画像のoverlayパラメータを返す。
    戻り値: (chara_path, scaled_w, scaled_h, base_x, base_y)
    chara_path が None のときはキャラなし。
    """
    chara_path = _find_chara_image(clip_type, page_idx, themes_dir)
    if chara_path is None:
        return None, 0, 0, 0, 0

    layout = cfg.get("layout", {})
    chara_top    = layout.get("chara_zone_top",    CHARA_Y1)
    chara_bottom = layout.get("chara_zone_bottom", CHARA_Y2)

    char_cfg  = cfg.get("character", {})
    scale_key = f"scale_{clip_type}"
    scale     = char_cfg.get(scale_key, char_cfg.get("scale", 0.5))

    chara = Image.open(chara_path).convert("RGBA")
    cw, ch = chara.size
    zone_h = chara_bottom - chara_top
    new_h  = min(int(ch * scale), zone_h)
    new_w  = int(cw * (new_h / ch))

    # 基本位置: 水平中央・ゾーン下端基準
    base_x = (vw - new_w) // 2
    base_y = chara_bottom - new_h

    # ピクセル単位オフセット（move_gen.toml の [character] で調整可）
    # offset_x: 正=右, 負=左  offset_y: 正=下, 負=上
    base_x += char_cfg.get("offset_x", 0)
    base_y += char_cfg.get("offset_y", 0)

    return chara_path, new_w, new_h, base_x, base_y


def _build_bg_crop_filter(
    bg_input_tag: str,
    vw: int,
    vh: int,
    crop_x: int,
    crop_y: int,
    out_tag: str = "[bg_cropped]",
) -> str:
    """
    背景画像をランダム開始座標で静止切り出しする filter_complex フラグメントを返す。
    パン（動き）は concat 後に _apply_pan_to_video() でまとめて適用する。
    """
    return f"{bg_input_tag}crop={vw}:{vh}:{crop_x}:{crop_y}{out_tag}"


def _build_chara_filter(
    cw: int, ch: int, cx: int, cy: int,
    ef: dict,
    clip_type: str,
    input_idx_bg: int = 0,
    input_idx_chara: int = 1,
    force_fade: bool = True,
    anim_type: str = "wobble",   # "none" | "wobble" | "spring"
    pos_offset_x: int = 0,       # 初期位置ランダムオフセット (3〜5px)
    pos_offset_y: int = 0,
) -> str:
    """
    キャラ画像に S&S / Spring / wobble / fade-in を適用する
    filter_complex フィルタ文字列を生成して返す。

    anim_type:
      "none"   → 揺れなし（初期位置オフセットのみ適用）
      "wobble" → 微揺れ（wobble_amp/freq をランダム範囲から選択済みの値で使用）
      "spring" → スプリング（spring_amp/decay/freq をランダム範囲から選択済みの値で使用）

    pos_offset_x/y: 初期位置に加算する3〜5pxのランダムオフセット
    """
    ax    = ef.get("wobble_amp_x",    1)
    ay    = ef.get("wobble_amp_y",    1)
    fx    = ef.get("wobble_freq_x",   0.5)
    fy    = ef.get("wobble_freq_y",   0.3)
    ss_x  = ef.get("squash_stretch_x", 0.06)
    ss_y  = ef.get("squash_stretch_y", 0.04)
    ss_f  = ef.get("squash_freq",      fx)
    sp_a  = ef.get("spring_amp",      12)
    sp_d  = ef.get("spring_decay",    3.0)
    sp_f  = ef.get("spring_freq",     6.0)

    # キャラ画像が切り替わったときのみフェードイン・Spring を有効化
    fd_key = f"fade_in_{clip_type}"
    fd   = ef.get(fd_key, ef.get("fade_in_body", 0.15)) if force_fade else 0.0

    # anim_type に応じてパラメータを上書き
    if anim_type == "none":
        ax = 0; ay = 0
        sp_a = 0.0
        ss_x = 0; ss_y = 0   # S&S も無効化
    elif anim_type == "spring":
        ax = 0; ay = 0        # 微揺れは無効、Springのみ
    else:  # "wobble"
        sp_a = 0.0            # Spring無効、微揺れのみ

    # spring は force_fade（キャラ切替）時のみ有効
    if not force_fade:
        sp_a = 0.0

    # S&S: scale で動的サイズ変更。中心固定は overlay x/y 式で実現
    dw_expr = f"{cw}*(1+{ss_x}*sin(t*6.28318*{ss_f}))"
    dh_expr = f"{ch}*(1-{ss_y}*sin(t*6.28318*{ss_f}))"

    # キャラ中心座標（スクリーン）＋初期位置ランダムオフセット
    center_x = cx + cw // 2 + pos_offset_x
    center_y = cy + ch // 2 + pos_offset_y

    ox = (f"{center_x}-trunc(({dw_expr})/2)"
          f"+sin(t*6.28318*{fx})*{ax}"
          f"+{sp_a}*exp(-{sp_d}*t)*sin(t*6.28318*{sp_f})")
    oy = (f"{center_y}-trunc(({dh_expr})/2)"
          f"+cos(t*6.28318*{fy})*{ay}")

    bg_tag    = f"[{input_idx_bg}:v]" if input_idx_bg >= 0 else "[_bg_input]"
    chara_tag = f"[{input_idx_chara}:v]"

    if fd > 0.0:
        chara_filter = (f"{chara_tag}format=rgba,"
                        f"scale=w='trunc({dw_expr})':h='trunc({dh_expr})':eval=frame,"
                        f"fade=t=in:st=0:d={fd}:alpha=1[_chara]")
    else:
        chara_filter = (f"{chara_tag}format=rgba,"
                        f"scale=w='trunc({dw_expr})':h='trunc({dh_expr})':eval=frame[_chara]")
    parts = [
        f"{bg_tag}format=rgba[_bg]",
        chara_filter,
        f"[_bg][_chara]overlay=x='{ox}':y='{oy}':eval=frame:format=auto[v]",
    ]
    return ";".join(parts)


def _generate_popin_frames(
    bg_path:     Path,
    sub_overlay: Path,
    work_dir:    Path,
    vw: int, vh: int,
    fps: int,
    duration: float,
    zoom_start: float,
    zoom_end:   float,
    zoom_dur:   float,
    cfg:         dict = None,
) -> Path:
    """
    introポップインアニメーション。
    bg_path はすでに vw×vh にクロップ済みの静止画を想定する。

    最適化方針（フレーム全生成を廃止）:
    - POPINアニメーション部分（zoom_dur_f枚）のみPillowで生成
    - POPIN後のidle部分は静止画ループ
    - 両者をconcatして音声をmux → 大幅な処理時間削減
    """
    zoom_dur_f = max(2, round(zoom_dur * fps))
    idle_dur   = max(0.0, duration - zoom_dur)

    # 背景+字幕を合成した等倍フレーム（1枚だけ）
    # bg_path はcrop済みvw×vhなのでそのまま使用
    bg = Image.open(bg_path).convert("RGBA").resize((vw, vh), Image.LANCZOS)
    sub = Image.open(sub_overlay).convert("RGBA")
    if sub.size != (vw, vh):
        sub = sub.resize((vw, vh), Image.LANCZOS)
    full_frame = Image.alpha_composite(bg, sub).convert("RGB")

    # ---- Part1: POPINフレームをPillowで生成（zoom_dur_f枚のみ）----
    frame_dir = work_dir / "_popin_frames"
    frame_dir.mkdir(exist_ok=True)

    for fi in range(zoom_dur_f):
        t_lin = fi / max(zoom_dur_f - 1, 1)
        t = 1 - (1 - t_lin) ** 2   # easeOutQuad
        z = zoom_start + (zoom_end - zoom_start) * t
        sw = max(2, int(vw * z) // 2 * 2)
        sh = max(2, int(vh * z) // 2 * 2)
        scaled = full_frame.resize((sw, sh), Image.LANCZOS)
        frame = Image.new("RGB", (vw, vh), (0, 0, 0))
        frame.paste(scaled, ((vw - sw) // 2, (vh - sh) // 2))
        frame.save(str(frame_dir / f"f{fi:04d}.png"))

    popin_clip = work_dir / "_popin_only.mp4"
    r = subprocess.run([
        "ffmpeg", "-y",
        "-framerate", str(fps), "-r", str(fps),
        "-i", str(frame_dir / "f%04d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-video_track_timescale", "12800",
        str(popin_clip),
    ], capture_output=True, encoding="utf-8", errors="replace")
    import shutil
    shutil.rmtree(str(frame_dir), ignore_errors=True)
    if r.returncode != 0:
        raise RuntimeError(f"[POPIN生成失敗]:\n{r.stderr[-400:]}")

    # ---- Part2: idle部分は合成済み静止画ループ ----
    full_png = work_dir / "_popin_full.png"
    full_frame.save(str(full_png))

    idle_clip = work_dir / "_popin_idle.mp4"
    r2 = subprocess.run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(full_png),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-video_track_timescale", "12800",
        "-t", str(idle_dur),
        str(idle_clip),
    ], capture_output=True, encoding="utf-8", errors="replace")
    full_png.unlink(missing_ok=True)
    if r2.returncode != 0:
        raise RuntimeError(f"[idle生成失敗]:\n{r2.stderr[-400:]}")

    # ---- Part3: POPIN + idle を filter_complex concat で結合 ----
    # (2クリップのみなのでコマンドライン長の問題なし)
    video_only = work_dir / "_popin_video.mp4"
    fc_str = "[0:v][1:v]concat=n=2:v=1:a=0[v]"
    r3 = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(popin_clip),
        "-i", str(idle_clip),
        "-filter_complex", fc_str,
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-video_track_timescale", "12800",
        str(video_only),
    ], capture_output=True, encoding="utf-8", errors="replace")
    popin_clip.unlink(missing_ok=True)
    idle_clip.unlink(missing_ok=True)
    if r3.returncode != 0:
        raise RuntimeError(f"[concat失敗]:\n{r3.stderr[-400:]}")

    return video_only


def _make_focus_overlay(
    out_path: Path,
    cfg:      dict,
    cx:       int,
    cy:       int,
) -> None:
    """
    集中線のみの透過PNGを out_path に保存する。
    背景画像は一切読み込まない。
    """
    ef  = cfg.get("effects", {})
    n   = ef.get("focus_lines",  24)
    alp = ef.get("focus_alpha", 120)
    col = _hex_to_rgb(ef.get("focus_color", "#FFFFFF"))
    lw  = ef.get("focus_width",   3)
    vw  = cfg["video"]["width"]
    vh  = cfg["video"]["height"]

    canvas = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
    od     = ImageDraw.Draw(canvas)
    max_r  = math.sqrt(vw * vw + vh * vh)

    for i in range(n):
        angle  = 2 * math.pi * i / n
        spread = math.pi * 0.012
        ex  = int(cx + math.cos(angle) * max_r)
        ey  = int(cy + math.sin(angle) * max_r)
        ex2 = int(cx + math.cos(angle + spread) * max_r)
        ey2 = int(cy + math.sin(angle + spread) * max_r)
        od.polygon([(cx, cy), (ex, ey), (ex2, ey2)], fill=col + (alp,))
        od.line([(cx, cy), (ex, ey)], fill=col + (min(255, alp + 60),), width=lw)

    canvas.save(str(out_path))

# ========================================
# クリップ生成
# ========================================

def _generate_clip(
    bg_path:       Path,
    audio_path:    Path,
    duration:      float,
    out_clip:      Path,
    text:          str,
    cfg:           dict,
    clip_type:     str,           # "intro" | "body" | "comment" | "jingle"
    work_dir:      Path,
    sound_effects: dict = None,   # find_sound_effects() の戻り値
    page_idx:      int  = 0,      # bodyページ番号（キャラ画像切替に使用）
    themes_dir:    str  = "",     # themes サブフォルダ名
    chara_fade:    bool = True,   # キャラ画像が切り替わったときのみ True
    pos_offset_x:  int  = 0,      # キャラ初期位置オフセット（run_entryで決定・全clip共通）
    pos_offset_y:  int  = 0,
    bg_pan_params: dict = None,   # 背景パンパラメータ（run_entryで決定・全clip共通）
    pan_start_time: float = 0.0,  # このクリップが動画全体の何秒目から始まるか
) -> None:
    """
    1セクション分のクリップを生成する。中間動画ファイルは生成しない。

    intro:   背景PNG + 字幕overlay + zoompanフィルタ → 音声合成（効果音: title）
    body:    背景PNG + 字幕overlay → -loop 1（効果音: ""含む行のみ important_word）
    comment: 背景PNG(zoom済み) + 集中線overlay + 字幕overlay → -loop 1（効果音: comment）
    """
    vw  = cfg["video"]["width"]
    vh  = cfg["video"]["height"]
    fps = cfg["video"]["fps"]
    ef  = cfg.get("effects", {})
    sc  = cfg.get("sound", {})

    # --------------------------------------------------
    # アニメーション種別・パラメータのランダム決定
    # 50% → なし / 30% → 微揺れ / 20% → スプリング
    # --------------------------------------------------
    _r = random.random()
    if _r < 0.50:
        _anim_type = "none"
    elif _r < 0.80:
        _anim_type = "wobble"
    else:
        _anim_type = "spring"

    # キャラ初期位置オフセット：外部から渡された値を使用（introで決定・全clip共通）
    _pos_ox = pos_offset_x
    _pos_oy = pos_offset_y

    # ランダム範囲でパラメータを上書き（efのコピーを作成して元を汚さない）
    ef = dict(ef)
    if _anim_type == "wobble":
        ef["wobble_amp_x"]  = random.uniform(0.0, 2.0)
        ef["wobble_amp_y"]  = random.uniform(0.0, 1.0)
        ef["wobble_freq_x"] = random.uniform(0.3, 0.6)
        ef["wobble_freq_y"] = random.uniform(0.2, 0.4)
    elif _anim_type == "spring":
        ef["spring_amp"]   = random.uniform(8.0,  14.0)
        ef["spring_decay"] = random.uniform(2.5,   3.5)
        ef["spring_freq"]  = random.uniform(5.5,   7.5)

    print(f"[アニメ] {clip_type}: type={_anim_type} pos_offset=({_pos_ox},{_pos_oy})")

    # --------------------------------------------------
    # 背景パンパラメータ：外部から渡された値を使用（全clip共通）
    # --------------------------------------------------
    if bg_pan_params:
        _bg_w        = bg_pan_params["bg_w"]
        _bg_h        = bg_pan_params["bg_h"]
        _base_x      = bg_pan_params["crop_x"]   # 動画開始時の初期切り出しX座標
        _base_y      = bg_pan_params["crop_y"]   # 動画開始時の初期切り出しY座標
        _pan_dx      = bg_pan_params["pan_dx"]
        _pan_dy      = bg_pan_params["pan_dy"]
        _pan_enabled = bg_pan_params["enabled"]
    else:
        _bg_img      = Image.open(bg_path)
        _bg_w, _bg_h = _bg_img.size
        _base_x = 0; _base_y = 0
        _pan_dx = 0.0; _pan_dy = 0.0
        _pan_enabled = False

    # このクリップの t=0 時点での切り出し座標
    _max_x   = max(0, _bg_w - vw)
    _max_y   = max(0, _bg_h - vh)
    _crop_x  = int(min(_max_x, max(0, _base_x + pan_start_time * _pan_dx)))
    _crop_y  = int(min(_max_y, max(0, _base_y + pan_start_time * _pan_dy)))

    print(f"[背景パン] crop=({_crop_x},{_crop_y}) start_time={pan_start_time:.2f}s dx={_pan_dx:.2f} dy={_pan_dy:.2f}")

    stem          = out_clip.stem
    sub_overlay   = work_dir / f"_sub_{stem}.png"
    focus_overlay = work_dir / f"_focus_{stem}.png"

    zoom_dur     = ef.get("intro_zoom_dur",   0.25)
    zoom_start   = ef.get("intro_zoom_start", 0.90)
    zoom_end     = ef.get("intro_zoom_end",   1.00)
    comment_zoom = ef.get("comment_zoom",     1.10)

    # --------------------------------------------------
    # 背景：run_entry で生成済みの全体背景動画を pan_start_time でシークして使う
    # bg_path はこの関数に渡された時点で全体背景動画(mp4)を指している
    # --------------------------------------------------
    def get_bg_input_args() -> list:
        """全体背景動画を pan_start_time からシークして読み込む ffmpeg 引数"""
        return ["-ss", f"{pan_start_time:.4f}", "-i", str(bg_path)]

    def _cropped_bg_image() -> "Image.Image":
        """pan_start_time 時点の背景フレームをPillowで取得する（popin/zoom用）"""
        _tmp_frame = work_dir / f"_bgframe_{stem}.png"
        subprocess.run([
            "ffmpeg", "-y", "-ss", f"{pan_start_time:.4f}",
            "-i", str(bg_path), "-vframes", "1", str(_tmp_frame)
        ], capture_output=True)
        img = Image.open(_tmp_frame).convert("RGB")
        _tmp_frame.unlink(missing_ok=True)
        return img

    se_path   = None
    se_volume = 1.0
    if sound_effects:
        if clip_type == "intro":
            se_path   = sound_effects.get("title")
            se_volume = sc.get("title_volume", 1.0)
        elif clip_type == "comment":
            se_path   = sound_effects.get("comment")
            se_volume = sc.get("comment_volume", 1.0)
        elif clip_type == "body":
            # ""が含まれるページのみ important_word を再生
            if re.search(r'"[^"]+"', text):
                se_path   = sound_effects.get("important_word")
                se_volume = sc.get("important_word_volume", 1.0)

    # --------------------------------------------------
    # audio_filter: 効果音がある場合は amix で合成
    # 効果音は先頭から1回再生し、クリップ長で切る（-t は映像側で制御）
    # --------------------------------------------------
    def build_audio_filter(voice_idx: int, se_idx: int) -> tuple[str, list]:
        """
        戻り値: (filter_complex追記分, 追加inputリスト)
        効果音なし → filter不要、追加inputなし
        効果音あり → amixフィルタ文字列と [-i se_path] を返す
        """
        if se_path is None or not se_path.exists():
            return None, []
        af = (
            f"[{voice_idx}:a]volume=1.0[voice];"
            f"[{se_idx}:a]volume={se_volume}[se];"
            f"[voice][se]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        return af, ["-i", str(se_path)]

    # --------------------------------------------------
    # Step 1: 字幕オーバーレイ生成（透過PNG・背景不要）
    # --------------------------------------------------
    # clip_type ごとに字幕スタイルを切り替え
    # wiki_config.toml に [subtitle_intro] / [subtitle_comment] があれば使用、
    # なければ共通の [subtitle] にフォールバック
    subtitle_key = {
        "intro":   "subtitle_intro",
        "body":    "subtitle",
        "comment": "subtitle_comment",
    }.get(clip_type, "subtitle")
    table_cx, table_cy = _make_subtitle_overlay(sub_overlay, text, cfg, subtitle_key)

    # --------------------------------------------------
    # Step 2: ffmpegで1発合成
    # --------------------------------------------------
    encode_args = _video_encode_args(cfg)

    if clip_type == "jingle":
        jingle_cfg = {k: v for k, v in cfg.items()}
        jingle_sub_cfg = dict(cfg.get("subtitle_intro", cfg["subtitle"]))
        jingle_sub_cfg["alignment"] = 5
        jingle_cfg["subtitle_intro"] = jingle_sub_cfg
        jingle_cfg["subtitle_intro_table"] = cfg.get("subtitle_intro_table", {})
        _make_subtitle_overlay(sub_overlay, text, jingle_cfg, "subtitle_intro")

        chara_path_j, cw_j, ch_j, cx_j, cy_j = _get_chara_overlay_params(
            "jingle", cfg, vw, vh, 0, themes_dir)
        if chara_path_j and ef.get("anim_enabled", True):
            chara_j = work_dir / f"_csj_{stem}.png"
            Image.open(chara_path_j).convert("RGBA").resize(
                (cw_j, ch_j), Image.LANCZOS).save(str(chara_j))
            jingle_fc = _build_chara_filter(
                cw_j, ch_j, cx_j, cy_j, ef, "jingle",
                input_idx_bg=0, input_idx_chara=2,
                force_fade=chara_fade, anim_type=_anim_type,
                pos_offset_x=_pos_ox, pos_offset_y=_pos_oy)
            sub_fc = "[1:v]format=rgba[_subj];[0:v][_subj]overlay=x=0:y=0:format=auto[bg_sub]"
            jingle_fc2 = jingle_fc.replace("[0:v]", "[bg_sub]")
            fc_j = f"{sub_fc};{jingle_fc2}"
            cmd_j = ["ffmpeg", "-y",
                     *get_bg_input_args(),              # 0: bg全体動画(-ss付き)
                     "-loop", "1", "-i", str(sub_overlay),  # 1: sub
                     "-loop", "1", "-i", str(chara_j),      # 2: chara
                     "-i",          str(audio_path),         # 3: audio
                     "-filter_complex", fc_j,
                     "-map", "[v]", "-map", "3:a",
                     *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                     "-t", str(duration), str(out_clip)]
            r = subprocess.run(cmd_j, capture_output=True, encoding="utf-8", errors="replace")
            chara_j.unlink(missing_ok=True)
        else:
            sub_fc = "[1:v]format=rgba[_subj2];[0:v][_subj2]overlay=x=0:y=0:format=auto[v]"
            cmd_j = ["ffmpeg", "-y",
                     *get_bg_input_args(),              # 0: bg全体動画(-ss付き)
                     "-loop", "1", "-i", str(sub_overlay),  # 1: sub
                     "-i",          str(audio_path),         # 2: audio
                     "-filter_complex", sub_fc,
                     "-map", "[v]", "-map", "2:a",
                     *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                     "-t", str(duration), str(out_clip)]
            r = subprocess.run(cmd_j, capture_output=True, encoding="utf-8", errors="replace")

    elif clip_type == "intro":
        # intro: body と同じ構造（全体背景動画 -ss シーク → sub → chara overlay）
        # popin は廃止（背景は全編パンのみ）
        ef_i = cfg.get("effects", {})
        chara_path_i, cw_i, ch_i, cx_i, cy_i = _get_chara_overlay_params(
            clip_type, cfg, vw, vh, page_idx, themes_dir)

        if chara_path_i and ef_i.get("anim_enabled", True):
            chara_i = work_dir / f"_ci_{stem}.png"
            Image.open(chara_path_i).convert("RGBA").resize(
                (cw_i, ch_i), Image.LANCZOS).save(str(chara_i))
            intro_fc = _build_chara_filter(
                cw_i, ch_i, cx_i, cy_i, ef_i, "intro",
                input_idx_bg=0, input_idx_chara=2,
                force_fade=chara_fade, anim_type=_anim_type,
                pos_offset_x=_pos_ox, pos_offset_y=_pos_oy)
            sub_fc = "[1:v]format=rgba[_subi];[0:v][_subi]overlay=x=0:y=0:format=auto[bg_sub_i]"
            intro_fc2 = intro_fc.replace("[0:v]", "[bg_sub_i]")
            af, se_inputs = build_audio_filter(voice_idx=3, se_idx=4)
            fc_str_i = f"{sub_fc};{intro_fc2}"
            fc_str_i += (";" + af) if af else ""
            map_audio_i = ["-map", "[aout]"] if af else ["-map", "3:a"]
            r = subprocess.run([
                "ffmpeg", "-y",
                *get_bg_input_args(),                  # 0: bg全体動画(-ss付き)
                "-loop", "1", "-i", str(sub_overlay),  # 1: sub
                "-loop", "1", "-i", str(chara_i),      # 2: chara
                "-i",          str(audio_path),         # 3: audio
                *se_inputs,
                "-filter_complex", fc_str_i,
                "-map", "[v]", *map_audio_i,
                *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                "-t", str(duration), str(out_clip),
            ], capture_output=True, encoding="utf-8", errors="replace")
            chara_i.unlink(missing_ok=True)
        else:
            af, se_inputs = build_audio_filter(voice_idx=2, se_idx=3)
            sub_fc = "[1:v]format=rgba[_subi2];[0:v][_subi2]overlay=x=0:y=0:format=auto[v]"
            fc_str_i = sub_fc + (";" + af if af else "")
            map_audio_i = ["-map", "[aout]"] if af else ["-map", "2:a"]
            r = subprocess.run([
                "ffmpeg", "-y",
                *get_bg_input_args(),                  # 0: bg全体動画(-ss付き)
                "-loop", "1", "-i", str(sub_overlay),  # 1: sub
                "-i",          str(audio_path),         # 2: audio
                *se_inputs,
                "-filter_complex", fc_str_i,
                "-map", "[v]", *map_audio_i,
                *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                "-t", str(duration), str(out_clip),
            ], capture_output=True, encoding="utf-8", errors="replace")

    elif clip_type == "comment":
        # comment: 全体背景動画 -ss シーク → 集中線overlay → sub → chara
        # zoom廃止（背景は全編パンのみ）・集中線は字幕前景として維持
        _make_focus_overlay(focus_overlay, cfg, cx=table_cx, cy=table_cy)

        ef_c = cfg.get("effects", {})
        chara_path_c, cw_c, ch_c, cx_c, cy_c = _get_chara_overlay_params(
            clip_type, cfg, vw, vh, page_idx, themes_dir)

        if chara_path_c and ef_c.get("anim_enabled", True):
            chara_c = work_dir / f"_csc_{stem}.png"
            Image.open(chara_path_c).convert("RGBA").resize(
                (cw_c, ch_c), Image.LANCZOS).save(str(chara_c))
            comment_fc = _build_chara_filter(
                cw_c, ch_c, cx_c, cy_c, ef_c, "comment",
                input_idx_bg=0, input_idx_chara=3,
                force_fade=chara_fade, anim_type=_anim_type,
                pos_offset_x=_pos_ox, pos_offset_y=_pos_oy)
            # bg(0) → 集中線(1) → sub(2) → chara(3)
            overlay_fc = (
                "[1:v]format=rgba[_focus];"
                "[0:v][_focus]overlay=x=0:y=0:format=auto[bg_focus];"
                "[2:v]format=rgba[_subc];"
                "[bg_focus][_subc]overlay=x=0:y=0:format=auto[bg_focus_sub]"
            )
            comment_fc2 = comment_fc.replace("[0:v]", "[bg_focus_sub]")
            af2, se_inputs2 = build_audio_filter(voice_idx=4, se_idx=5)
            fc2 = f"{overlay_fc};{comment_fc2}"
            fc2 += (";" + af2) if af2 else ""
            map_audio2 = ["-map", "[aout]"] if af2 else ["-map", "4:a"]
            r = subprocess.run([
                "ffmpeg", "-y",
                *get_bg_input_args(),                  # 0: bg全体動画(-ss付き)
                "-loop", "1", "-i", str(focus_overlay), # 1: 集中線
                "-loop", "1", "-i", str(sub_overlay),   # 2: sub
                "-loop", "1", "-i", str(chara_c),       # 3: chara
                "-i",          str(audio_path),          # 4: audio
                *se_inputs2,
                "-filter_complex", fc2,
                "-map", "[v]", *map_audio2,
                *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                "-t", str(duration), str(out_clip),
            ], capture_output=True, encoding="utf-8", errors="replace")
            chara_c.unlink(missing_ok=True)
        else:
            af2_static, se_inputs2_static = build_audio_filter(voice_idx=3, se_idx=4)
            overlay_fc = (
                "[1:v]format=rgba[_focus2];"
                "[0:v][_focus2]overlay=x=0:y=0:format=auto[bg_focus2];"
                "[2:v]format=rgba[_subc2];"
                "[bg_focus2][_subc2]overlay=x=0:y=0:format=auto[v]"
            )
            fc2_s = overlay_fc + (";" + af2_static if af2_static else "")
            map_a2 = ["-map", "[aout]"] if af2_static else ["-map", "3:a"]
            r = subprocess.run([
                "ffmpeg", "-y",
                *get_bg_input_args(),                  # 0: bg全体動画(-ss付き)
                "-loop", "1", "-i", str(focus_overlay), # 1: 集中線
                "-loop", "1", "-i", str(sub_overlay),   # 2: sub
                "-i",          str(audio_path),          # 3: audio
                *se_inputs2_static,
                "-filter_complex", fc2_s,
                "-map", "[v]", *map_a2,
                *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                "-t", str(duration), str(out_clip),
            ], capture_output=True, encoding="utf-8", errors="replace")
        focus_overlay.unlink(missing_ok=True)

    else:
        # body: 全体背景動画を -ss でシーク → sub → chara overlay
        ef = cfg.get("effects", {})
        chara_path, cw, ch, cx, cy = _get_chara_overlay_params(
            clip_type, cfg, vw, vh, page_idx, themes_dir)

        if chara_path and cfg.get("effects", {}).get("anim_enabled", True):
            chara_scaled = work_dir / f"_cs_{stem}.png"
            Image.open(chara_path).convert("RGBA").resize(
                (cw, ch), Image.LANCZOS).save(str(chara_scaled))
            chara_fc = _build_chara_filter(
                cw, ch, cx, cy, ef, "body",
                input_idx_bg=0, input_idx_chara=2,
                force_fade=chara_fade, anim_type=_anim_type,
                pos_offset_x=_pos_ox, pos_offset_y=_pos_oy)
            sub_fc = "[1:v]format=rgba[_subb];[0:v][_subb]overlay=x=0:y=0:format=auto[bg_sub]"
            body_fc = chara_fc.replace("[0:v]", "[bg_sub]")
            af_overlay, se_inputs_overlay = build_audio_filter(voice_idx=3, se_idx=4)
            fc_str = f"{sub_fc};{body_fc}"
            fc_str += (";" + af_overlay) if af_overlay else ""
            map_audio = ["-map", "[aout]"] if af_overlay else ["-map", "3:a"]
            cmd = [
                "ffmpeg", "-y",
                *get_bg_input_args(),                  # 0: bg全体動画(-ss付き)
                "-loop", "1", "-i", str(sub_overlay),  # 1: sub
                "-loop", "1", "-i", str(chara_scaled),  # 2: chara
                "-i",          str(audio_path),          # 3: audio
                *se_inputs_overlay,
                "-filter_complex", fc_str,
                "-map", "[v]", *map_audio,
                *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                "-t", str(duration), str(out_clip),
            ]
            r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
            chara_scaled.unlink(missing_ok=True)
        else:
            af_static, se_inputs_static = build_audio_filter(voice_idx=2, se_idx=3)
            sub_fc = "[1:v]format=rgba[_subb2];[0:v][_subb2]overlay=x=0:y=0:format=auto[v]"
            fc_str = sub_fc + (";" + af_static if af_static else "")
            map_audio = ["-map", "[aout]"] if af_static else ["-map", "2:a"]
            cmd = [
                "ffmpeg", "-y",
                *get_bg_input_args(),                  # 0: bg全体動画(-ss付き)
                "-loop", "1", "-i", str(sub_overlay),  # 1: sub
                "-i",          str(audio_path),          # 2: audio
                *se_inputs_static,
                "-filter_complex", fc_str,
                "-map", "[v]", *map_audio,
                *encode_args, "-c:a", "aac", "-pix_fmt", "yuv420p",
                "-t", str(duration), str(out_clip),
            ]
            r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")

    sub_overlay.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(f"[クリップ生成失敗] {clip_type}:\n{r.stderr[-600:]}")

    print(f"[クリップ] {clip_type} 完了: {out_clip.name}")

# ========================================
# クリップ結合 + BGM
# ========================================

def _make_jingle_clip(
    bg_path:    Path,
    cfg:        dict,
    clip_dir:   Path,
    seq:        int,
    work_dir:   Path,
    themes_dir: str = "",
) -> Path | None:
    """
    「ぷにっと解説！」ジングルクリップを生成して返す。
    intro_voice.wav が存在しない場合は None を返してスキップ。

    仕様:
    - テキスト「ぷにっと解説！」を subtitle_intro フォント・画面中央に表示
    - 音声: 0.12秒の無音 + intro_voice.wav（合計長が表示時間）
    - ポップインなし（静止画）
    """
    voice_path = INTRO_VOICE_PATH
    if not voice_path.exists():
        print(f"[ジングル] {voice_path.name} が見つかりません → スキップ")
        return None

    print(f"[ジングル] {voice_path.name} を使用")

    # 0.12秒無音 + intro_voice.wav を結合した wav を生成
    import wave as _wave
    with _wave.open(str(voice_path), "r") as wf:
        params    = wf.getparams()
        vf_frames = wf.readframes(wf.getnframes())

    silence_frames = b"\x00" * int(
        params.framerate * params.nchannels * params.sampwidth * 0.12
    )
    jingle_wav = work_dir / "_jingle.wav"
    with _wave.open(str(jingle_wav), "w") as wf:
        wf.setparams(params)
        wf.writeframes(silence_frames + vf_frames)

    duration = get_audio_duration_ffprobe(jingle_wav)

    out = clip_dir / f"{seq:03d}_jingle_00.mp4"
    _generate_clip(
        bg_path, jingle_wav, duration, out,
        "ぷにっと解説！", cfg, "jingle", work_dir,
        themes_dir=themes_dir,
    )
    jingle_wav.unlink(missing_ok=True)
    return out


# _build_fade_vf は廃止（フェードインはキャラoverlayのalphaで実現）


def _concat_and_mix(clips: list, bgm_path, output_path: Path,
                    cfg: dict, work_dir: Path,
                    bg_pan_params: dict = None) -> None:
    # 全クリップをfilter_complex concatで結合
    n = len(clips)
    inputs = []
    for p in clips:
        inputs += ["-i", str(p)]
    fc_in  = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    fc_str = f"{fc_in}concat=n={n}:v=1:a=1[v][a]"
    concat_path = work_dir / "concat.mp4"
    r = subprocess.run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", fc_str,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(concat_path),
    ], capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"[結合失敗]:\n{r.stderr[-600:]}")

    if bgm_path and bgm_path.exists():
        bgm_vol = cfg.get("bgm", {}).get("volume", 0.2)
        r = subprocess.run([
            "ffmpeg", "-y",
            "-i", str(concat_path),
            "-stream_loop", "-1", "-i", str(bgm_path),
            "-filter_complex",
            f"[1:a]volume={bgm_vol}[bgm];[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v", "-map", "[aout]",
            *_video_encode_args(cfg),
            "-c:a", "aac",
            str(output_path),
        ], capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"[BGMミックス失敗]:\n{r.stderr[-600:]}")
        concat_path.unlink(missing_ok=True)
    else:
        shutil.move(str(concat_path), str(output_path))

# ========================================
# 1エントリの動画生成
# ========================================

def run_entry(entry: WikiEntry, cfg: dict, use_voicevox: bool,
              bgm_path, run_idx: int, total: int) -> Path:
    print(f"\n{'='*50}")
    print(f"  [{run_idx}/{total}] id={entry.id}  「{entry.name}」")
    print(f"{'='*50}")
    _entry_start = _time.perf_counter()

    safe     = _safe_filename(f"{entry.id}_{entry.name}")
    work_dir = WORK_DIR / safe
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    # 出力ファイルが既に存在する場合はスキップ（重複エントリ・再実行による上書き防止）
    output_path_check = RESULT_DIR / f"{safe}.mp4"
    if output_path_check.exists():
        print(f"[スキップ] 出力ファイルが既に存在します: {output_path_check.name}")
        return output_path_check

    work_dir.mkdir(parents=True, exist_ok=True)

    vid   = entry.id
    title = entry.name

    # ----------------------------------------------------------------
    # アセットチェック（背景・キャラ）
    # 不足がある場合はエラーとして即終了する
    # ----------------------------------------------------------------
    t_asset = _time.perf_counter()
    bg_missing    = False
    chara_missing = False

    # 背景画像チェック
    themes_dir_name = entry.themes.strip() if entry.themes.strip() else ""
    bg_path = None

    # ① output/work/{id}.png を最優先で使用
    work_bg_path = WORK_DIR / f"{entry.id}.png"
    if work_bg_path.exists():
        bg_path = work_bg_path
        print(f"[背景] work画像: {bg_path.name}")

    # ② themes サブフォルダ
    if bg_path is None and themes_dir_name:
        sub_bg_dir = BG_DIR / themes_dir_name
        if sub_bg_dir.exists():
            sub_candidates = sorted(
                p for p in sub_bg_dir.iterdir()
                if p.suffix.lower() in IMAGE_EXTENSIONS
            )
            if sub_candidates:
                bg_path = sub_candidates[0]
                print(f"[背景] themes={themes_dir_name} -> {bg_path.name}")

    # ③ id対応
    if bg_path is None:
        bg_path = find_bg_for_id(entry.id)
        if bg_path is not None:
            print(f"[背景] id対応: {bg_path.name}")

    # ④ フォールバック
    if bg_path is None:
        candidates = sorted(
            p for p in BG_DIR.iterdir()
            if p.suffix.lower() in IMAGE_EXTENSIONS
        ) if BG_DIR.exists() else []
        if candidates:
            bg_path = candidates[0]
            print(f"[背景] フォールバック: {bg_path.name}")
        else:
            bg_missing = True
            print(f"[エラー] 背景画像が見つかりません: {BG_DIR}")

    # キャラ画像チェック（intro/body_01/comment の3種すべて確認）
    for clip_type in ("intro", "body", "comment"):
        if _find_chara_image(clip_type, 0, themes_dir_name) is None:
            chara_missing = True
            print(f"[エラー] キャラ画像が見つかりません: clip_type={clip_type}")
            break

    # asset_missing_flag の決定
    if bg_missing and chara_missing:
        asset_flag = "bg+chara"
    elif bg_missing:
        asset_flag = "bg"
    elif chara_missing:
        asset_flag = "chara"
    else:
        asset_flag = "none"

    asset_dur = _time.perf_counter() - t_asset

    if asset_flag != "none":
        err_msg = f"必要なアセットが不足しています: {asset_flag}"
        write_process_log(
            LOG_DIR, video_id=vid, process="asset_check",
            status="error", title=title,
            duration_sec=asset_dur, error_message=err_msg,
        )
        write_movie_gen_log(
            LOG_DIR, video_id=vid, title=title,
            status="failed",
            duration_sec=_time.perf_counter() - _entry_start,
            voicevox_status="ok" if use_voicevox else "skipped",
            asset_missing_flag=asset_flag,
            render_status="failed",
            error_message=err_msg,
        )
        raise RuntimeError(err_msg)

    write_process_log(
        LOG_DIR, video_id=vid, process="asset_check",
        status="success", title=title, duration_sec=asset_dur,
    )

    # ----------------------------------------------------------------
    # Step 1: 音声生成
    # ----------------------------------------------------------------
    print("\n[Step 1] 音声生成")
    audio_dir = work_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    def split_pages(text: str) -> list:
        if "|" in text:
            return [p.strip() for p in text.split("|") if p.strip()]
        return [text]

    intro_pages   = split_pages(entry.intro)
    body_pages    = split_pages(entry.body)
    comment_pages = split_pages(entry.comment)

    def gen_pages_audio(pages: list, prefix: str) -> list:
        results = []
        for pi, page in enumerate(pages):
            wav = audio_dir / f"{prefix}_{pi:02d}.wav"
            gen_audio(page, wav, use_voicevox, cfg)
            append_silence(wav, SILENCE_SEC)
            dur = get_audio_duration_ffprobe(wav)
            results.append((wav, dur))
        return results

    t_audio = _time.perf_counter()
    try:
        intro_audio   = gen_pages_audio(intro_pages,   "intro")
        body_audio    = gen_pages_audio(body_pages,     "body")
        comment_audio = gen_pages_audio(comment_pages,  "comment")
    except Exception as e:
        audio_dur = _time.perf_counter() - t_audio
        err_msg   = str(e)
        write_process_log(
            LOG_DIR, video_id=vid, process="audio_generate",
            status="failed", title=title,
            duration_sec=audio_dur, error_message=err_msg,
        )
        write_movie_gen_log(
            LOG_DIR, video_id=vid, title=title,
            status="failed",
            duration_sec=_time.perf_counter() - _entry_start,
            voicevox_status="ok" if use_voicevox else "skipped",
            asset_missing_flag=asset_flag,
            render_status="failed",
            error_message=err_msg,
        )
        raise

    audio_dur      = _time.perf_counter() - t_audio
    total_audio_sec = (
        sum(d for _, d in intro_audio)
        + sum(d for _, d in body_audio)
        + sum(d for _, d in comment_audio)
    )
    write_process_log(
        LOG_DIR, video_id=vid, process="audio_generate",
        status="success", title=title,
        duration_sec=audio_dur,
    )

    # ----------------------------------------------------------------
    # Step 2: クリップ生成
    # ----------------------------------------------------------------
    print("\n[Step 2] クリップ生成")
    clip_dir = work_dir / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)

    sound_effects = find_sound_effects()
    all_clips     = []
    clip_seq      = 0
    prev_chara_path = None

    # ----------------------------------------------------------------
    # クリップ共通パラメータをここで1回だけ決定
    # ① キャラ初期位置オフセット（introで決定、全clip共通）
    # ② 背景パンパラメータ（背景が小さい場合はパディングして余白を作る）
    # ----------------------------------------------------------------
    _chara_pos_ox = random.randint(3, 5) * random.choice([-1, 1])
    _chara_pos_oy = random.randint(3, 5) * random.choice([-1, 1])
    print(f"[キャラ位置] pos_offset=({_chara_pos_ox},{_chara_pos_oy}) ← 全clip共通")

    # ----------------------------------------------------------------
    # 背景パン用パラメータを決定
    # ・パン余白は200px固定（視覚的にわかる動きを保証）
    # ・パン速度 = 動画総時間で余白の70%を移動する値に自動計算
    # ・初期位置は余白の中央付近からランダムにずらす
    # ----------------------------------------------------------------
    vw = cfg["video"]["width"]
    vh = cfg["video"]["height"]
    _PAN_MARGIN = 200   # 各辺に追加するパン余白(px)

    _orig_bg = Image.open(bg_path).convert("RGB")
    _orig_bw, _orig_bh = _orig_bg.size

    # 常にパン余白付きの大きめ背景を作成
    # 元画像を _pad_bw × _pad_bh にリサイズするだけ（貼り重ね不要）
    _pad_bw = vw + _PAN_MARGIN * 2
    _pad_bh = vh + _PAN_MARGIN * 2
    _padded_bg_path = work_dir / f"_bg_padded_{vid}.png"
    _orig_bg.resize((_pad_bw, _pad_bh), Image.LANCZOS).save(str(_padded_bg_path))
    _pan_bw, _pan_bh = _pad_bw, _pad_bh
    print(f"[背景パン] 余白追加: {_orig_bw}x{_orig_bh} → {_pad_bw}x{_pad_bh}")
    bg_path = _padded_bg_path

    _margin_x = _pan_bw - vw   # = _PAN_MARGIN * 2
    _margin_y = _pan_bh - vh   # = _PAN_MARGIN * 2

    # 8方向ランダム
    _pan_dirs = [
        ( 1,  0), (-1,  0), ( 0,  1), ( 0, -1),
        ( 1,  1), ( 1, -1), (-1,  1), (-1, -1),
    ]
    _pan_dir = random.choice(_pan_dirs)

    # 速度：動画全体で余白の70%を移動する（端でクランプされにくい）
    _travel_x = _margin_x * 0.7 if _pan_dir[0] != 0 else 0.0
    _travel_y = _margin_y * 0.7 if _pan_dir[1] != 0 else 0.0
    _pan_dx = (_pan_dir[0] * _travel_x / total_audio_sec) if total_audio_sec > 0 and _travel_x > 0 else 0.0
    _pan_dy = (_pan_dir[1] * _travel_y / total_audio_sec) if total_audio_sec > 0 and _travel_y > 0 else 0.0

    # 初期位置：余白の中央から開始（移動方向と逆側に寄せて端クランプを防ぐ）
    # dx > 0 なら左寄り（crop_x = 0付近）、dx < 0 なら右寄り（crop_x = margin付近）
    _crop_x = (_PAN_MARGIN // 4) if _pan_dir[0] >= 0 else (_margin_x - _PAN_MARGIN // 4)
    _crop_y = (_PAN_MARGIN // 4) if _pan_dir[1] >= 0 else (_margin_y - _PAN_MARGIN // 4)
    # さらにランダムに±20px揺らす
    _crop_x = max(0, min(_margin_x, _crop_x + random.randint(-20, 20)))
    _crop_y = max(0, min(_margin_y, _crop_y + random.randint(-20, 20)))

    _bg_pan_params = {
        "bg_w":    _pan_bw,
        "bg_h":    _pan_bh,
        "crop_x":  _crop_x,
        "crop_y":  _crop_y,
        "pan_dx":  _pan_dx,
        "pan_dy":  _pan_dy,
        "enabled": True,
    }
    print(f"[背景パン] crop=({_crop_x},{_crop_y}) dir={_pan_dir} dx={_pan_dx:.2f}px/s dy={_pan_dy:.2f}px/s total={total_audio_sec:.1f}s")

    # ----------------------------------------------------------------
    # 背景動画を動画全体で1本だけ事前生成
    # セクションの影響を受けず、global_time ベースで連続したパンを保証する
    # ----------------------------------------------------------------
    _full_bg_video_path = work_dir / f"_bg_full_{vid}.mp4"
    print(f"[背景パン] 全体背景動画を生成中: {total_audio_sec:.1f}s ...")

    _full_bg_img = Image.open(bg_path).convert("RGB")
    if NUMPY_AVAILABLE:
        _full_bg_arr = np.array(_full_bg_img)
        def _get_bg_frame(cx, cy):
            return _full_bg_arr[cy:cy + vh, cx:cx + vw].tobytes()
    else:
        def _get_bg_frame(cx, cy):
            return _full_bg_img.crop((cx, cy, cx + vw, cy + vh)).tobytes()

    _bg_max_x = max(0, _pan_bw - vw)
    _bg_max_y = max(0, _pan_bh - vh)
    _bg_total_frames = max(1, int(round(total_audio_sec * cfg["video"]["fps"])))
    _PAN_START_DELAY = 0.2   # パン開始までの静止時間（秒）

    _bg_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{vw}x{vh}", "-pix_fmt", "rgb24",
        "-r", str(cfg["video"]["fps"]),
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-video_track_timescale", "12800",
        str(_full_bg_video_path),
    ]
    _bg_proc = subprocess.Popen(_bg_cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _fi in range(_bg_total_frames):
        _gt = max(0.0, _fi / cfg["video"]["fps"] - _PAN_START_DELAY)   # 0.2秒後からパン開始
        _bx = int(min(_bg_max_x, max(0, _crop_x + _pan_dx * _gt)))
        _by = int(min(_bg_max_y, max(0, _crop_y + _pan_dy * _gt)))
        if _fi % (cfg["video"]["fps"] * 2) == 0:   # 2秒ごとにログ
            print(f"  [bg_frame] global_time={_fi/cfg['video']['fps']:.2f}s  pan_time={_gt:.2f}s  x={_bx}  y={_by}")
        _bg_proc.stdin.write(_get_bg_frame(_bx, _by))
    _bg_proc.stdin.close()
    _bg_proc.wait()
    if _bg_proc.returncode != 0:
        raise RuntimeError("[背景動画生成失敗]")
    print(f"[背景パン] 全体背景動画生成完了: {_full_bg_video_path.name}")

    _pan_elapsed_sec = 0.0   # intro先頭からの経過秒数（クリップのシーク位置に使用）

    def gen_page_clips(pages, audio_list, clip_type):
        nonlocal clip_seq, prev_chara_path, _pan_elapsed_sec
        clips = []
        for pi, (page, (wav, dur)) in enumerate(zip(pages, audio_list)):
            out = clip_dir / f"{clip_seq:03d}_{clip_type}_{pi:02d}.mp4"
            cur_chara_path = _find_chara_image(clip_type, pi, themes_dir_name)
            chara_fade = (cur_chara_path != prev_chara_path)
            print(f"[chara_fade] {clip_type}_{pi:02d}: prev={prev_chara_path and prev_chara_path.name} cur={cur_chara_path and cur_chara_path.name} fade={chara_fade}")
            print(f"[背景パン] {clip_type}_{pi:02d}: pan_start_time={_pan_elapsed_sec:.2f}s")

            _generate_clip(_full_bg_video_path, wav, dur, out, page, cfg, clip_type, work_dir,
                           sound_effects=sound_effects, page_idx=pi,
                           themes_dir=themes_dir_name, chara_fade=chara_fade,
                           pos_offset_x=_chara_pos_ox, pos_offset_y=_chara_pos_oy,
                           bg_pan_params=_bg_pan_params,
                           pan_start_time=_pan_elapsed_sec)
            prev_chara_path = cur_chara_path
            clips.append(out)
            clip_seq += 1
            _pan_elapsed_sec += dur
        return clips

    t_clip = _time.perf_counter()
    try:
        all_clips += gen_page_clips(intro_pages,   intro_audio,   "intro")
        all_clips += gen_page_clips(body_pages,    body_audio,    "body")
        all_clips += gen_page_clips(comment_pages, comment_audio, "comment")
    except Exception as e:
        clip_dur = _time.perf_counter() - t_clip
        err_msg  = str(e)
        write_process_log(
            LOG_DIR, video_id=vid, process="clip_generate",
            status="failed", title=title,
            duration_sec=clip_dur, error_message=err_msg,
        )
        write_movie_gen_log(
            LOG_DIR, video_id=vid, title=title,
            status="failed",
            duration_sec=_time.perf_counter() - _entry_start,
            voicevox_status="ok" if use_voicevox else "skipped",
            audio_duration=total_audio_sec,
            asset_missing_flag=asset_flag,
            render_status="failed",
            error_message=err_msg,
        )
        raise

    clip_dur = _time.perf_counter() - t_clip
    write_process_log(
        LOG_DIR, video_id=vid, process="clip_generate",
        status="success", title=title, duration_sec=clip_dur,
    )

    # ----------------------------------------------------------------
    # Step 3: 結合 + BGM
    # ----------------------------------------------------------------
    print("\n[Step 3] 結合・BGMミックス")
    output_path = RESULT_DIR / f"{safe}.mp4"

    t_render = _time.perf_counter()
    try:
        _concat_and_mix(all_clips, bgm_path, output_path, cfg, work_dir,
                        bg_pan_params=_bg_pan_params)
    except Exception as e:
        render_dur = _time.perf_counter() - t_render
        err_msg    = str(e)
        write_process_log(
            LOG_DIR, video_id=vid, process="video_render",
            status="failed", title=title,
            duration_sec=render_dur, error_message=err_msg,
        )
        write_movie_gen_log(
            LOG_DIR, video_id=vid, title=title,
            status="failed",
            duration_sec=_time.perf_counter() - _entry_start,
            voicevox_status="ok" if use_voicevox else "skipped",
            audio_duration=total_audio_sec,
            asset_missing_flag=asset_flag,
            render_status="failed",
            error_message=err_msg,
        )
        raise

    render_dur = _time.perf_counter() - t_render
    write_process_log(
        LOG_DIR, video_id=vid, process="video_render",
        status="success", title=title, duration_sec=render_dur,
    )

    # 動画ファイルの存在を確認してから一時ファイルを削除
    if not output_path.exists():
        raise RuntimeError(f"[動画生成失敗] 出力ファイルが見つかりません: {output_path}")

    shutil.rmtree(work_dir, ignore_errors=True)

    # output/work/{id}.png（背景）を削除
    work_bg_path.unlink(missing_ok=True)
    if not work_bg_path.exists():
        print(f"[背景] 削除完了: {work_bg_path.name}")

    entry_elapsed = _time.perf_counter() - _entry_start
    print(f"\n[完了] {output_path}  (合計: {entry_elapsed:.1f}s)")

    # ----------------------------------------------------------------
    # サマリーログ（成功）
    # ----------------------------------------------------------------
    write_movie_gen_log(
        LOG_DIR, video_id=vid, title=title,
        status="success",
        duration_sec=entry_elapsed,
        voicevox_status="ok" if use_voicevox else "skipped",
        audio_duration=total_audio_sec,
        asset_missing_flag=asset_flag,
        render_status="success",
    )

    return output_path

# ========================================
# メイン
# ========================================

def main(channel: str = None, max_count: int = None):
    """
    Args:
        channel: チャンネル名（例: nazolabo）。None の場合はコマンドライン引数から取得
        max_count: 処理する動画の最大本数。None の場合は status=1 の全件処理
                   （単体起動時のデフォルト）。manager.py から上限値を渡す。
    """
    # チャンネル別パスを取得
    if channel is None:
        # グローバル定義されたパスを使用（コマンドライン引数で --channel が指定される場合）
        parser = argparse.ArgumentParser(description="なぜラボ Wiki Shorts 動画生成")
        parser.add_argument("--channel", type=str, required=True, help="チャンネル名（例: nazolabo）")
        parser.add_argument("--no-voicevox", action="store_true",
                            help="音声生成をスキップ（ダミー音声）")
        parser.add_argument("--count",  type=int, default=0,
                            help="生成本数（0=wiki_config.tomlの video.count）")
        parser.add_argument("--reset",  action="store_true",
                            help="sequential進捗をリセットして先頭から")
        parser.add_argument("--id",     default="",
                            help="特定IDのみ生成（例: --id 001）")
        args = parser.parse_args()
        channel = args.channel
    else:
        # manager.py から呼ばれた場合は sys.argv から --channel を除外して解析
        parser = argparse.ArgumentParser(description="なぜラボ Wiki Shorts 動画生成")
        parser.add_argument("--no-voicevox", action="store_true",
                            help="音声生成をスキップ（ダミー音声）")
        parser.add_argument("--count",  type=int, default=0,
                            help="生成本数（0=wiki_config.tomlの video.count）")
        parser.add_argument("--reset",  action="store_true",
                            help="sequential進捗をリセットして先頭から")
        parser.add_argument("--id",     default="",
                            help="特定IDのみ生成（例: --id 001）")
        args = parser.parse_args()

    # チャンネル別パスを動的に定義
    DATA_DIR   = get_channel_data_dir(channel)
    OUTPUT_DIR = get_channel_output_dir(channel)
    LOG_DIR    = get_channel_logs_dir(channel)

    BG_DIR     = DATA_DIR / "background"
    BGM_DIR    = DATA_DIR / "bgm"
    SOUND_DIR  = DATA_DIR / "sound"
    CHARA_DIR  = DATA_DIR / "characters"
    RESULT_DIR = OUTPUT_DIR / "result"
    WORK_DIR   = OUTPUT_DIR / "work"

    GENKOU_CSV_PATH       = DATA_DIR / "genkou.csv"
    INTRO_VOICE_PATH      = SOUND_DIR / "intro_voice.wav"

    print("=" * 50)
    print("  なぜラボ Wiki Shorts 動画生成システム")
    print("=" * 50)

    cfg = load_config()

    if not PIL_AVAILABLE:
        print("[エラー] Pillowが未インストールです。pip install Pillow")
        sys.exit(1)
    print("[環境] Pillow: OK")

    use_voicevox = not args.no_voicevox
    if use_voicevox:
        t_vv = _time.perf_counter()
        if check_voicevox():
            vv_dur = _time.perf_counter() - t_vv
            print("[環境] VOICEVOX: OK")
            write_process_log(
                LOG_DIR, video_id="", process="voicevox_check",
                status="success", title=VOICEVOX_URL, duration_sec=vv_dur,
            )
        else:
            vv_dur = _time.perf_counter() - t_vv
            msg = f"VOICEVOXに接続できません ({VOICEVOX_URL})"
            print(f"[エラー] {msg}")
            print("  VOICEVOX を起動してから再実行してください。")
            print("  音声生成をスキップする場合は --no-voicevox オプションを使用してください。")
            write_process_log(
                LOG_DIR, video_id="", process="voicevox_check",
                status="error", title=VOICEVOX_URL,
                duration_sec=vv_dur, error_message=msg,
            )
            sys.exit(1)
    else:
        write_process_log(
            LOG_DIR, video_id="", process="voicevox_check",
            status="skipped", title="--no-voicevox",
        )

    if not check_ffmpeg():
        print("[エラー] ffmpegが見つかりません。インストールしてください。")
        sys.exit(1)
    print("[環境] ffmpeg: OK")

    print("\n[Step 0] 原稿データ読み込み")
    all_entries = load_genkou(Path(args.csv))

    # 処理対象エントリ決定
    if args.id:
        target = [e for e in all_entries if e.id == args.id]
        if not target:
            print(f"[エラー] id={args.id} が見つかりません")
            sys.exit(1)
        entries   = target
        start_idx = None
    else:
        # themes.csv の status==1（原稿生成済み）または status==2（背景生成済み）の name と一致するエントリを全件処理
        pending_names = _load_pending_themes()
        if not pending_names:
            print("[themes] status=1 のエントリがありません。終了します。")
            sys.exit(0)
        entries = [e for e in all_entries if e.name in pending_names]
        if not entries:
            print("[themes] status=1 のエントリが genkou.csv に見つかりません。終了します。")
            sys.exit(0)
        start_idx = None
        count = len(entries)
        print(f"[themes] status=1: {len(pending_names)}件 / genkou.csv 一致: {len(entries)}件を処理")

    # max_count が指定されている場合は先頭から上限本数に切り詰める
    if max_count is not None and len(entries) > max_count:
        print(f"[設定] max_count={max_count} (manager指定) / 全件={len(entries)} -> {max_count}件に絞り込み")
        entries = entries[:max_count]

    bgm_path = find_bgm()
    if bgm_path is None:
        print("[BGM] なし（BGMなしで生成）")

    results = []
    for i, entry in enumerate(entries, 1):
        try:
            out = run_entry(entry, cfg, use_voicevox, bgm_path, i, len(entries))
            results.append((entry.id, str(out), "成功"))
            if start_idx is not None:
                _save_seq_index(start_idx + i)
            # themes.csv の該当行を動画生成済み(status=3)に更新
            t_th = _time.perf_counter()
            update_themes_status(entry.name, 3)
            write_process_log(
                LOG_DIR, video_id=entry.id, process="themes_update",
                status="success", title=entry.name,
                duration_sec=_time.perf_counter() - t_th,
            )
        except Exception as e:
            import traceback
            print(f"\n[エラー] id={entry.id}: {e}")
            traceback.print_exc()
            results.append((entry.id, "-", f"失敗: {e}"))
            # themes.csv の該当行を失敗(status=93)に更新
            try:
                update_themes_status(entry.name, 93)
            except Exception:
                pass

    print(f"\n{'='*50}")
    print(f"  全{len(entries)}件の処理が完了しました")
    print(f"{'='*50}")
    for eid, path, status in results:
        print(f"  id={eid}: {status}")
        if status == "成功":
            print(f"       -> {path}")


if __name__ == "__main__":
    main()
