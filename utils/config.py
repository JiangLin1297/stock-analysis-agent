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

# ── 配置缓存（基于文件修改时间） ──────────────────────────
_config_cache = None
_config_mtime = 0.0


def load_config() -> dict:
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if _config_cache is not None and mtime == _config_mtime:
            return _config_cache
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            _config_cache = json.load(f)
        _config_mtime = mtime
        return _config_cache
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(data: dict) -> None:
    global _config_cache, _config_mtime
    existing = load_config()
    existing.update(data)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    # 写入后更新缓存
    _config_cache = existing
    try:
        _config_mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        _config_mtime = 0.0


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
