"""MockAdapter:离线闭环用,无需密钥。

默认产出一个合法的 Artifact JSON。可通过构造参数注入脚本化行为,
用于测试 JSON 自修复(F-D.3)与成本熔断(F-D.6)。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from backend.core.model_adapter.base import ModelAdapter
from backend.schema import InvokeResult


def _default_artifact_json(messages: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "role": "backend-engineer-agent",
            "task_id": "demo",
            "status": "done",
            "artifact": {
                "files": ["backend/example.py"],
                "summary": "MockAdapter 产出的示例交付物",
            },
            "handoff_notes": "M1 闭环验证用",
            "issues": [],
            "open_questions": [],
        },
        ensure_ascii=False,
    )


class MockAdapter(ModelAdapter):
    """脚本化模型。

    - responder: 给定 messages 返回字符串内容;默认产出合法 Artifact JSON。
    - tokens_in/tokens_out: 每次调用记账的 token 数(测试熔断用)。
    """

    def __init__(
        self,
        responder: Callable[[list[dict[str, str]]], str] | None = None,
        tokens_in: int = 100,
        tokens_out: int = 200,
    ) -> None:
        self._responder = responder or _default_artifact_json
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.call_count = 0

    def invoke(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
        tier: str = "large",
    ) -> InvokeResult:
        self.call_count += 1
        content = self._responder(messages)
        return InvokeResult(
            content=content,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            latency_ms=1,
        )
