"""HostCommand:多渠道归一指令 DTO + 意图初判(M01 / F-A.1)。

各渠道(飞书/Web)消息归一为统一 HostCommand,屏蔽渠道差异下发编排器。
意图初判用轻量规则:edit 关键词命中 → edit,否则 runtime(小模型分类留 G9)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Channel = Literal["lark", "web"]
Intent = Literal["runtime", "edit"]

# 改系统自身(Edit)的意图关键词;命中则初判 edit。
# M6/F-E.7:纳管范围扩至当前项目全量代码(含 backend/frontend),
# 故"改 web / 改样式 / 改颜色 / 改前端 / 改 UI / 改后端"等改"系统自身"的诉求
# 也应路由到 Edit(经回归 + Host 确认的受控链路),而非当作 Runtime 业务交付。
_EDIT_HINTS = (
    "改系统", "改自己", "改流程", "改工作流", "改角色", "改提示词",
    "edit 系统", "自迭代", "改 agent", "改 workflow", "改 skill", "改 tool",
    # M6/F-E.7:改当前项目代码(前端/后端/样式)
    "改web", "改 web", "修改web", "修改 web", "改前端", "改后端",
    "改样式", "改 style", "改style", "改ui", "改 ui", "改界面",
    "改颜色", "改配色", "改主题", "改代码", "改页面",
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
    # F-A.9 多轮上下文:本 session 的历史消息([{role:host|assistant,text,ts}])。
    messages: list[dict[str, Any]] = field(default_factory=list)
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
            "messages": self.messages,
            "reply_to": self.reply_to,
        }


def classify_intent(text: str) -> Intent:
    """意图初判(F-A.1):轻量规则,edit 关键词命中走 edit,否则 runtime。"""
    low = text.lower()
    for hint in _EDIT_HINTS:
        if hint.lower() in low:
            return "edit"
    return "runtime"


# 对话式决策(F-A.7 通道①):Host 直接用文字回复 need_decision,
# 入口层把自然语言归一为 verdict(pass|reject|abort);无法判定返回 None,
# 交由界面内联按钮兜底,不擅自替 Host 决策。
_DECISION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("abort", ("终止", "中止", "取消", "停止", "放弃", "别做了", "abort", "stop")),
    ("reject", ("打回", "返工", "重做", "重来", "不行", "不对", "驳回",
                "再改", "继续改", "reject")),
    ("pass", ("通过", "放行", "可以", "同意", "确认", "没问题", "ok", "好的",
              "合并", "merge", "approve", "pass", "lgtm")),
)


def classify_decision(text: str) -> str | None:
    """把 Host 的对话式文字回复解析为决策 verdict(F-A.7 通道①)。"""
    low = (text or "").strip().lower()
    if not low:
        return None
    # abort/reject 优先级高于 pass(避免"不通过"被误判为 pass)。
    for verdict, hints in _DECISION_HINTS:
        for h in hints:
            if h in low:
                return verdict
    return None
