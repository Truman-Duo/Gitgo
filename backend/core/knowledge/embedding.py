"""EmbeddingProvider —— provider-agnostic 的 embedding 抽象。

v0.35 Phase 2: 复用 LLMProvider 的抽象模式。
           默认不可用（需配置 embedding_provider + embedding_model）。
           支持 OpenAI / 兼容 API。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import TYPE_CHECKING


class EmbeddingProvider:
    """Provider-agnostic embedding。默认未配置。"""

    def __init__(self, provider: str = "", model: str = "",
                 base_url: str = "", api_key: str = ""):
        self._provider = provider or os.environ.get("GITGO_EMBEDDING_PROVIDER", "")
        self._model = model or os.environ.get("GITGO_EMBEDDING_MODEL", "")
        self._base_url = base_url or os.environ.get("GITGO_EMBEDDING_BASE_URL",
                                                     "https://api.openai.com/v1")
        self._api_key = api_key or os.environ.get("GITGO_EMBEDDING_API_KEY", "")

    @property
    def available(self) -> bool:
        return bool(self._provider and self._model)

    def embed(self, text: str) -> list[float] | None:
        """调用 embedding API。"""
        if not self.available or not text.strip():
            return None

        body = json.dumps({
            "model": self._model,
            "input": text,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self._base_url.rstrip('/')}/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["data"][0]["embedding"]
        except Exception:
            return None
