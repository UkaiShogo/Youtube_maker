#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
image_checker.py
生成済み画像の品質チェックモジュール
配置先: wikiproject/scripts/image_checker.py

【themes.csv status 関連】
  ※ 本モジュールは status を直接更新しない
  ※ image_generator.py が呼び出し元で、status管理を行う
     - status=1 で画像生成開始
     - 品質OK → status=2 に更新
     - 品質NG × MAX_IMAGE_ATTEMPTS → status=92 に更新

【チェック仕様】
  1. ファイルサイズ  : MIN_FILE_SIZE 未満 → 破損/空ファイルの可能性 → NG
  2. ピクセル輝度std : STD_THRESHOLD 未満 → 単色/ほぼ単色画像 → NG
     (Pillowがない場合はサイズのみで判定)

【用途】
  image_generator.py から呼び出す。
  NGの場合は再生成リトライ → 超過時に status=92（画像生成失敗スキップ）を設定。
"""

from __future__ import annotations
from pathlib import Path

# ── デフォルト閾値 ──────────────────────────────────────
MIN_FILE_SIZE  = 50_000   # bytes（50KB未満は疑わしい）
STD_THRESHOLD  = 15.0     # グレースケール輝度の標準偏差（これ未満は単色判定）


def check_image(
    path: Path,
    min_file_size: int   = MIN_FILE_SIZE,
    std_threshold: float = STD_THRESHOLD,
) -> tuple[bool, str]:
    """
    画像ファイルの品質チェックを行う。

    Args:
        path:           チェック対象の画像ファイルパス
        min_file_size:  最小ファイルサイズ（バイト）
        std_threshold:  輝度標準偏差の最低閾値

    Returns:
        (ok: bool, reason: str)
          ok=True  → 品質OK
          ok=False → 品質NG（reason に理由が入る）
    """
    # ── ファイル存在確認 ──────────────────────────────────
    if not path.exists():
        return False, f"ファイルが存在しない: {path}"

    # ── ファイルサイズチェック ────────────────────────────
    file_size = path.stat().st_size
    if file_size < min_file_size:
        return False, (
            f"ファイルサイズ不足 ({file_size:,} bytes < {min_file_size:,} bytes)"
        )

    # ── Pillow + NumPy によるピクセル品質チェック ─────────
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        # ライブラリ未インストール時はサイズ判定のみでOKとする
        return True, ""

    try:
        img = Image.open(path).convert("L")       # グレースケール変換
        arr = np.array(img, dtype=np.float32)
        std = float(arr.std())

        if std < std_threshold:
            return False, (
                f"単色/低品質画像 (輝度std={std:.1f} < 閾値{std_threshold})"
            )

        return True, ""

    except Exception as e:
        return False, f"画像読み込みエラー: {e}"


# ── 単体テスト ───────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("使用法: python image_checker.py <画像パス>")
        sys.exit(1)

    target = Path(sys.argv[1])
    ok, reason = check_image(target)
    if ok:
        print(f"[OK] {target.name}")
    else:
        print(f"[NG] {target.name} -- {reason}")
    sys.exit(0 if ok else 1)
