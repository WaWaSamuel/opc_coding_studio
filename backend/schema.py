"""核心数据结构(PRD 第二部分 核心数据结构 + 第五部分 M02/M03)。

只放 M1 用到的字段;Edit/记忆/并行等字段留到后续里程碑。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArtifactStatus(str, Enum):
    DONE = "done"
    BLOCKED = "blocked"
    NEED_REWORK = "need_rework"


class TaskStatus(str, Enum):
    RUNNING = "running"
    NEED_DECISION = "need_decision"
    DONE = "done"
    FAILED = "failed"


class ArtifactBody(BaseModel):
    files: list[str] = Field(default_factory=list)
    summary: str = ""


class Artifact(BaseModel):
    """角色间唯一的结构化交接物(F-B.9)。"""

    role: str
    task_id: str
    status: ArtifactStatus
    artifact: ArtifactBody = Field(default_factory=ArtifactBody)
    handoff_notes: str = ""
    issues: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class CompanyState(BaseModel):
    """任务级状态;落 DB 即 Checkpoint(F-D.4)。"""

    task_id: str
    system: str = "runtime"  # runtime | edit(edit 在 M5)
    workflow: str = ""
    current_role: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[Artifact] = Field(default_factory=list)
    todo_plan: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    loop_counters: dict[str, int] = Field(default_factory=dict)
    retry_counters: dict[str, int] = Field(default_factory=dict)
    task_tokens: int = 0  # F-D.6 成本熔断累加
    transition: str = ""  # 借 Claude Code:记录"为何流转至此"
    memory_refs: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.RUNNING


class RoleSpec(BaseModel):
    """角色定义(F-C.1 三层提示词 + 输出 schema + 模型档位)。"""

    model_config = ConfigDict(protected_namespaces=())

    role_id: str
    model_tier: str = "large"  # large | small
    common_prompt: str = ""
    role_prompt: str = ""
    output_schema: dict[str, Any] = Field(default_factory=dict)


class InvokeResult(BaseModel):
    """ModelAdapter 调用结果(含 token 记账,F-A.6)。"""

    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out
