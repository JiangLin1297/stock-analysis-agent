#!/usr/bin/env python3
"""
DeepSeek V4 API 客户端封装 — 线程安全单例模式。
支持本地部署、硅基流动(SiliconFlow)及其他 OpenAI 兼容 API。

用法:
    from data.deepseek import deepseek_chat          # 向后兼容
    from data.deepseek import DeepSeekClient          # 推荐单例

    reply = deepseek_chat("你是技术分析师", "分析贵州茅台的技术面")
    # 或
    reply = DeepSeekClient().chat("你是技术分析师", "分析贵州茅台的技术面")

配置（优先级: 环境变量 > config.json > 默认值）:
    DEEPSEEK_BASE_URL    - API 地址，默认 https://api.deepseek.com
    DEEPSEEK_MODEL       - 模型名，默认 deepseek-v4-pro
    DEEPSEEK_API_KEY     - API Key
    DEEPSEEK_TEMPERATURE - 温度参数，默认 0.7
    DEEPSEEK_MAX_TOKENS  - 最大输出 token，默认 8192
    DEEPSEEK_TIMEOUT     - 请求超时秒数，默认 120

在 GUI「设置」页面可直接配置以上所有参数，保存后自动生效。
"""

import os
import json
import threading
import requests
from typing import Optional, Dict, Any

# ── 默认配置 ──────────────────────────────────────────────
_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-v4-pro"
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_TIMEOUT = 30
_DEFAULT_CONNECT_TIMEOUT = 8
_MAX_RETRIES = 2
_RETRY_BACKOFF = [2, 4]  # 指数退避秒数


def _load_config_from_file():
    """从 utils.config 加载配置（优先环境变量，其次 config.json，最后默认值）。"""
    try:
        from utils.config import get_config_value
        return {
            "base_url": os.environ.get("DEEPSEEK_BASE_URL") or get_config_value("DEEPSEEK_BASE_URL") or _DEFAULT_BASE_URL,
            "model": os.environ.get("DEEPSEEK_MODEL") or get_config_value("DEEPSEEK_MODEL") or _DEFAULT_MODEL,
            "api_key": os.environ.get("DEEPSEEK_API_KEY") or get_config_value("DEEPSEEK_API_KEY") or "",
            "temperature": float(os.environ.get("DEEPSEEK_TEMPERATURE") or get_config_value("DEEPSEEK_TEMPERATURE") or _DEFAULT_TEMPERATURE),
            "max_tokens": int(os.environ.get("DEEPSEEK_MAX_TOKENS") or get_config_value("DEEPSEEK_MAX_TOKENS") or _DEFAULT_MAX_TOKENS),
            "timeout": int(os.environ.get("DEEPSEEK_TIMEOUT") or get_config_value("DEEPSEEK_TIMEOUT") or _DEFAULT_TIMEOUT),
        }
    except Exception:
        return {
            "base_url": os.environ.get("DEEPSEEK_BASE_URL", _DEFAULT_BASE_URL),
            "model": os.environ.get("DEEPSEEK_MODEL", _DEFAULT_MODEL),
            "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
            "temperature": float(os.environ.get("DEEPSEEK_TEMPERATURE", str(_DEFAULT_TEMPERATURE))),
            "max_tokens": int(os.environ.get("DEEPSEEK_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS))),
            "timeout": int(os.environ.get("DEEPSEEK_TIMEOUT", str(_DEFAULT_TIMEOUT))),
        }


class DeepSeekClient:
    """
    线程安全单例：封装所有 DeepSeek API 配置和请求逻辑。
    无模块级全局变量，配置受 threading.Lock() 保护。
    """
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._config_lock = threading.Lock()
        self._load_config()
        self._initialized = True

    def _load_config(self):
        cfg = _load_config_from_file()
        with self._config_lock:
            self.base_url = cfg["base_url"]
            self.model = cfg["model"]
            self.api_key = cfg["api_key"]
            self.temperature = cfg["temperature"]
            self.max_tokens = cfg["max_tokens"]
            self.timeout = cfg["timeout"]

    def reload_config(self):
        """重新加载配置（用户在设置页修改后调用）。"""
        self._load_config()

    def chat(
        self,
        system_prompt: str,
        user_content: str,
        **kwargs
    ) -> str:
        """
        调用 DeepSeek V4 API（OpenAI 兼容接口），带自动重试。

        Args:
            system_prompt: 系统级指令，定义角色和行为。
            user_content:  用户消息内容，如分析报告、数据等。
            **kwargs:      可选覆盖参数:
                model       - 模型名称
                temperature - 采样温度 (0-2)
                max_tokens  - 最大输出 token 数
                base_url    - API 地址
                api_key     - API 密钥
                timeout     - 请求超时秒数(单值=read timeout; tuple=(connect, read))
                extra_body  - 额外请求体字段 (dict)

        Returns:
            str: 模型回复文本。

        Raises:
            requests.HTTPError: HTTP 错误（非超时）。
            ValueError: API Key 未设置。
        """
        with self._config_lock:
            base_url = kwargs.get('base_url', self.base_url)
            model = kwargs.get('model', self.model)
            api_key = kwargs.get('api_key', self.api_key)
            temperature = kwargs.get('temperature', self.temperature)
            max_tokens = kwargs.get('max_tokens', self.max_tokens)
            timeout = kwargs.get('timeout', self.timeout)
        extra_body = kwargs.get('extra_body', {})

        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY 未设置。请设置环境变量:\n"
                "  set DEEPSEEK_API_KEY=sk-xxxxxxxx\n"
                "或在系统环境变量中永久配置。"
            )

        # 超时处理：支持单值和元组
        if isinstance(timeout, (int, float)):
            connect_to = _DEFAULT_CONNECT_TIMEOUT
            read_to = timeout
        else:
            connect_to, read_to = timeout

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {api_key}"

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            **extra_body,
        }

        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=(connect_to, read_to))
                resp.raise_for_status()
                data = resp.json()
                msg = data["choices"][0]["message"]
                content = msg.get("content", "")
                if not content:
                    content = msg.get("reasoning_content", "")
                return content
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout,
                    requests.exceptions.ConnectionError) as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF[attempt]
                    import time as _time
                    _time.sleep(wait)
            except Exception as e:
                # 非网络错误（HTTP 4xx/5xx 等），不重试
                raise

        raise last_error

    def chat_stream(
        self,
        system_prompt: str,
        user_content: str,
        **kwargs
    ):
        """
        流式调用 DeepSeek V4，逐块 yield 文本增量。
        参数同 chat。
        """
        with self._config_lock:
            base_url = kwargs.get('base_url', self.base_url)
            model = kwargs.get('model', self.model)
            api_key = kwargs.get('api_key', self.api_key)
            temperature = kwargs.get('temperature', self.temperature)
            max_tokens = kwargs.get('max_tokens', self.max_tokens)
            timeout = kwargs.get('timeout', self.timeout)
        extra_body = kwargs.get('extra_body', {})

        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY 未设置。请设置环境变量:\n"
                "  set DEEPSEEK_API_KEY=sk-xxxxxxxx\n"
                "或在系统环境变量中永久配置。"
            )

        url = f"{base_url.rstrip('/')}/chat/completions"

        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {api_key}"

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **extra_body,
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=(5, timeout), stream=True)
        resp.raise_for_status()

        for line in resp.iter_lines():
            if not line:
                continue
            line = line.decode('utf-8')
            if line.startswith('data: '):
                chunk = line[6:]
                if chunk == '[DONE]':
                    break
                try:
                    delta = json.loads(chunk)['choices'][0].get('delta', {})
                    if 'content' in delta:
                        yield delta['content']
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


# ═══════════════════════════════════════════════════════════════
# 向后兼容的模块级函数（委托给单例）
# ═══════════════════════════════════════════════════════════════

def deepseek_chat(system_prompt: str, user_content: str, **kwargs) -> str:
    """向后兼容：委托给 DeepSeekClient 单例。"""
    return DeepSeekClient().chat(system_prompt, user_content, **kwargs)


def deepseek_chat_stream(system_prompt: str, user_content: str, **kwargs):
    """向后兼容：委托给 DeepSeekClient 单例。"""
    yield from DeepSeekClient().chat_stream(system_prompt, user_content, **kwargs)


def reload_config():
    """向后兼容：委托给 DeepSeekClient 单例。"""
    DeepSeekClient().reload_config()


# ── 硅基流动适配 ──────────────────────────────────────────
def configure_siliconflow(api_key: str, model: str = "deepseek-ai/DeepSeek-V3"):
    """
    一键配置硅基流动 API。

    Args:
        api_key: 硅基流动 API Key
        model:   模型 ID，默认 deepseek-ai/DeepSeek-V3
    """
    client = DeepSeekClient()
    with client._config_lock:
        client.base_url = "https://api.siliconflow.cn/v1"
        client.model = model
        client.api_key = api_key
    print(f"[deepseek_client] 已切换至硅基流动: {client.base_url} | {client.model}")


def configure_local(url: str = "http://localhost:8000/v1", model: str = "deepseek-v4"):
    """
    一键配置本地部署。

    Args:
        url:   本地服务地址
        model: 模型名
    """
    client = DeepSeekClient()
    with client._config_lock:
        client.base_url = url
        client.model = model
        client.api_key = ""
    print(f"[deepseek_client] 已切换至本地部署: {client.base_url} | {client.model}")


# ── 模块导入时初始化单例 ──────────────────────────────────
DeepSeekClient()


# ── 自检 ──────────────────────────────────────────────────
if __name__ == '__main__':
    client = DeepSeekClient()
    print(f"DeepSeek Client 配置:")
    print(f"  Base URL: {client.base_url}")
    print(f"  Model:    {client.model}")
    print(f"  API Key:  {'已设置' if client.api_key else '未设置（请在 GUI 设置页或环境变量中配置）'}")
    print(f"  Timeout:  {client.timeout}s")
    print()
    print("测试连接...")
    try:
        reply = deepseek_chat(
            "用一句话回答用户问题。",
            "请说'连接成功'"
        )
        print(f"  响应: {reply[:200]}")
    except Exception as e:
        print(f"  连接失败: {e}")
        print(f"  提示: 请设置环境变量 DEEPSEEK_API_KEY，然后重试")
