#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
path_helper.py
チャンネル別パス解決ユーティリティ

各スクリプトで BASE_DIR を廃止し、このモジュール経由でパスを取得
"""

from pathlib import Path
from typing import Optional


def get_base_dir() -> Path:
    """プロジェクトルートディレクトリを取得"""
    return Path(__file__).resolve().parent.parent


def get_channel_dir(channel: str) -> Path:
    """チャンネルディレクトリを取得"""
    return get_base_dir() / "channels" / channel


def get_channel_config_dir(channel: str) -> Path:
    """チャンネルの config ディレクトリを取得"""
    return get_channel_dir(channel) / "config"


def get_channel_prompts_dir(channel: str) -> Path:
    """チャンネルの prompts ディレクトリを取得"""
    return get_channel_dir(channel) / "prompts"


def get_channel_data_dir(channel: str) -> Path:
    """チャンネルの data ディレクトリを取得"""
    return get_channel_dir(channel) / "data"


def get_channel_output_dir(channel: str) -> Path:
    """チャンネルの output ディレクトリを取得"""
    return get_channel_dir(channel) / "output"


def get_channel_logs_dir(channel: str) -> Path:
    """チャンネルの logs ディレクトリを取得"""
    return get_channel_dir(channel) / "logs"


def get_common_dir() -> Path:
    """共通ディレクトリを取得"""
    return get_base_dir() / "common"


def get_common_config_dir() -> Path:
    """共通の config ディレクトリを取得"""
    return get_common_dir() / "config"


def get_common_assets_dir() -> Path:
    """共通の assets ディレクトリを取得"""
    return get_common_dir() / "assets"


def get_scripts_dir() -> Path:
    """scripts ディレクトリを取得"""
    return get_base_dir() / "scripts"


# ──────────────────────────────────────
# よく使うファイルパスのヘルパー関数
# ──────────────────────────────────────

def get_themes_csv(channel: str) -> Path:
    """themes.csv のパスを取得"""
    return get_channel_data_dir(channel) / "themes.csv"


def get_genkou_csv(channel: str) -> Path:
    """genkou.csv のパスを取得"""
    return get_channel_data_dir(channel) / "genkou.csv"


def get_upload_log_csv(channel: str) -> Path:
    """upload_log.csv のパスを取得"""
    return get_channel_data_dir(channel) / "upload_log.csv"


def get_theme_system_prompt(channel: str) -> Path:
    """theme_system.txt のパスを取得"""
    return get_channel_prompts_dir(channel) / "theme_system.txt"


def get_theme_user_prompt(channel: str) -> Path:
    """theme_user.txt のパスを取得"""
    return get_channel_prompts_dir(channel) / "theme_user.txt"


def get_genkou_prompt(channel: str) -> Path:
    """genkou_prompt.txt のパスを取得"""
    return get_channel_prompts_dir(channel) / "genkou_prompt.txt"


def get_theme_gen_config(channel: str) -> Path:
    """theme_gen_config.txt のパスを取得"""
    return get_channel_config_dir(channel) / "theme_gen_config.txt"


def get_genkou_checker_config(channel: str) -> Path:
    """genkou_checker_config.ini のパスを取得"""
    return get_channel_config_dir(channel) / "genkou_checker_config.ini"


def get_image_prompt_ini(channel: str) -> Path:
    """image_prompt.ini のパスを取得"""
    return get_channel_config_dir(channel) / "image_prompt.ini"


def get_uploader_config(channel: str) -> Path:
    """uploader_config.ini のパスを取得"""
    return get_channel_config_dir(channel) / "uploader_config.ini"


def get_manager_config(channel: str) -> Path:
    """manager_config.ini のパスを取得"""
    return get_channel_config_dir(channel) / "manager_config.ini"


def get_youtube_oauth_token() -> Path:
    """YouTube OAuth token.pickle のパスを取得（共通）"""
    return get_common_config_dir() / "token.pickle"


def get_youtube_client_secret() -> Path:
    """YouTube client_secret.json のパスを取得（共通）"""
    return get_common_config_dir() / "client_secret.json"


def get_anthropic_api_key_file() -> Path:
    """Anthropic API_key.txt のパスを取得（共通）"""
    return get_common_config_dir() / "API_key.txt"
