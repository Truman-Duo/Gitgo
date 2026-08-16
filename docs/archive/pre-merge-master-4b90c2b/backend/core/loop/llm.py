"""LLMProvider — OpenAI-compatible /chat/completions wrapper.

零新依赖——使用标准库 urllib。用于 daemon llm_call 命令。
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error


class LLMProvider:
    """OpenAI-compatible /chat/completions wrapper.

    不绑定特定模型——model_id + base_url + api_key 全部由 config 注入。
    """

    def __init__(self, base_url: str, api_key: str, model_id: str):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model_id

    def chat(self, messages: list[dict], max_tokens: int = 4096,
             timeout: int = 120) -> str:
        """同步 chat completion。返回 response content 文本。"""
        body = json.dumps({
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"LLM API error {e.code}: {error_body[:500]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"LLM API connection failed: {e.reason}"
            ) from e
