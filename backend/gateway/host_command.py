"""HostCommand:多渠道归一指令 DTO + 意图初判(M01 / F-A.1)。

各渠道(飞书/Web)消息归一为统一 HostCommand,屏蔽渠道差异下发编排器。
意图初判用轻量规则:edit 关键词命中 → edit,否则 runtime(小模型分类留 G9)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Channel = Literal["lark", "web"]
Intent = Literal["runtime", "edit"]

# 改系统自身(Edit)的意图关键词;命中则初判 edit(M5 才真正接 EditGraph)。
_EDIT_HINTS = (
    "改系统", "改自己", "改流程", "改工作流", "改角色", "改提示词",
    "edit 系统", "自迭代", "改 agent", "改 workflow", "改 skill", "改 tool",
)


@dataclass
class HostCommand:
    """编排器入口的统一指令(对齐 PRD 5.5 / 入口详设)。"""

    channel: Channel
    session_id: str
    text: str
    host_verified: bool = False
    intent: Intent = "runtime"
    attachments: list[dict[str, Any]] = field(default_factory=list)
    reply_to: str = ""        # 渠道侧回信锚点(飞书 chat_id / message_id 等)
    raw: dict[str, Any] = field(default_factory=dict)  # 渠道原始上下文(去重/审计)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "session_id": self.session_id,
            "host_verified": self.host_verified,
            "intent": self.intent,
            "text": self.text,
            "attachments": self.attachments,
            "reply_to": self.reply_to,
        }


def classify_intent(text: str) -> Intent:
    """意图初判(F-A.1):轻量规则,edit 关键词命中走 edit,否则 runtime。"""
    low = text.lower()
    for hint in _EDIT_HINTS:
        if hint.lower() in low:
            return "edit"
    return "runtime"
