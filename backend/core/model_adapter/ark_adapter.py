"""ArkAdapter:豆包/火山方舟,OpenAI 兼容接口(F-F.1)。

密钥仅经 ARK_* 环境变量(config.settings)。响应强制 JSON object(F-D.3)。
API 5xx/限流做指数退避重试,上限后抛 ModelCallError。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from backend.config import settings
from backend.core.model_adapter.base import ModelAdapter
from backend.errors import ModelCallError
from backend.schema import InvokeResult


class ArkAdapter(ModelAdapter):
    def __init__(self) -> None:
        if not settings.ark_api_key:
            raise ModelCallError("ARK_API_KEY 未配置(应经环境变量注入)")
        self._base_url = settings.ark_base_url.rstrip("/")
        self._api_key = settings.ark_api_key

    def _model_for(self, tier: str) -> str:
        if tier == "small" and settings.ark_model_small:
            return settings.ark_model_small
        return settings.ark_model

    def invoke(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
        tier: str = "large",
    ) -> InvokeResult:
        body: dict[str, Any] = {
            "model": self._model_for(tier),
            "messages": messages,
        }
        if schema is not None:
            # SchemaEnforcer:强制 JSON object 输出
            body["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{self._base_url}/chat/completions"

        last_err: Exception | None = None
        for attempt in range(settings.max_api_overload_retry):
            start = time.monotonic()
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(url, json=body, headers=headers)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise ModelCallError(f"ark {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                latency_ms = int((time.monotonic() - start) * 1000)
                choice = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return InvokeResult(
                    content=choice,
                    tokens_in=usage.get("prompt_tokens", 0),
                    tokens_out=usage.get("completion_tokens", 0),
                    latency_ms=latency_ms,
                )
            except (httpx.HTTPError, ModelCallError) as exc:
                last_err = exc
                # 指数退避
                time.sleep(min(2**attempt, 8))

        raise ModelCallError(f"ark 调用失败(重试 {settings.max_api_overload_retry} 次): {last_err}")
