#!/usr/bin/env python3
"""配置持久化管理 — 读写 config.json，优先环境变量。"""
import os
import sys
import json


def _data_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_FILE = os.path.join(_data_dir(), "config.json")


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(data: dict) -> None:
    existing = load_config()
    existing.update(data)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def get_config_value(key: str, default: str = "") -> str:
    """优先级: 环境变量 > config.json > default"""
    env_val = os.environ.get(key, "")
    if env_val:
        return env_val
    cfg = load_config()
    return cfg.get(key, default)


def set_config_value(key: str, value: str) -> None:
    """同时写入 os.environ 和 config.json。"""
    os.environ[key] = value
    save_config({key: value})
