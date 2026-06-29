"""ModelAdapter 抽象(F-F.1)。

统一接口 invoke(messages, schema, tier) -> InvokeResult;屏蔽模型差异。
切换模型只换实现,不改上层(PRD M04 边界)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from backend.schema import InvokeResult


class ModelAdapter(ABC):
    """所有模型适配器的统一接口。"""

    @abstractmethod
    def invoke(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
        tier: str = "large",
    ) -> InvokeResult:
        """调用模型。schema 非空时强制 JSON 输出(F-D.3)。"""
        raise NotImplementedError
