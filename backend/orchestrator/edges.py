"""EdgeRouter:按 Artifact 字段决定下一跳(F-B.1/F-B.9 条件边)。

把"读 status / verdict → 决定 pass/reject/need_rework/error/need_decision"的
机械判定收口到一处。LangGraph 的条件边在 M4 接入时复用同一套判定语义;
M2 串行编排直接消费这些 Decision。
"""
from __future__ import annotations

from enum import Enum

from backend.schema import Artifact, ArtifactStatus


class Decision(str, Enum):
    PASS = "pass"            # 继续前进到下一节点
    REWORK = "rework"        # 业务回退到上游重做(走 LoopController)
    NEED_DECISION = "need_decision"  # 交 Host 拍板(超限/阻塞)
    ERROR = "error"          # 上报错误


def decide_from_status(artifact: Artifact) -> Decision:
    """执行类节点产出后的机械判定:status 字段驱动。"""
    if artifact.status == ArtifactStatus.DONE:
        return Decision.PASS
    if artifact.status == ArtifactStatus.NEED_REWORK:
        return Decision.REWORK
    if artifact.status == ArtifactStatus.BLOCKED:
        return Decision.NEED_DECISION
    return Decision.ERROR


def decide_from_verdict(verdict: str) -> Decision:
    """Loop 判定节点产出后的机械判定:verdict=pass|reject。"""
    return Decision.PASS if verdict == "pass" else Decision.REWORK
