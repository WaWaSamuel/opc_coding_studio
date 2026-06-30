"""Repository 抽象(PRD M08)。

接口抽象,M1 用 SQLite 实现,后续可换 PostgreSQL 不改上层。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from backend.schema import CompanyState


class Repository(ABC):
    # --- 任务状态 / Checkpoint(F-D.4) ---
    @abstractmethod
    def save_checkpoint(self, state: CompanyState) -> None: ...

    @abstractmethod
    def load_checkpoint(self, task_id: str) -> CompanyState | None: ...

    @abstractmethod
    def list_checkpoints(self, limit: int = 100) -> list[dict[str, Any]]: ...

    # --- 流转日志 / 成本记账(F-A.6) ---
    @abstractmethod
    def append_log(self, entry: dict[str, Any]) -> None: ...

    @abstractmethod
    def logs_for(self, task_id: str) -> list[dict[str, Any]]: ...

    # --- 成本熔断:日累计(F-D.6) ---
    @abstractmethod
    def add_daily_tokens(self, day: str, tokens: int) -> int: ...

    @abstractmethod
    def get_daily_tokens(self, day: str) -> int: ...

    # --- Artifact 落库 / 按指针取回(F-C.3 三级流水线 ①:大产出 demand-paging) ---
    @abstractmethod
    def save_artifact(self, task_id: str, ref: str, content: str) -> None: ...

    @abstractmethod
    def load_artifact(self, ref: str) -> str | None: ...

    # --- 长期记忆读写 + 关键词检索(F-C.4 命名空间隔离 / F-C.5 检索注入) ---
    @abstractmethod
    def save_memory(self, namespace: str, kind: str, text: str) -> None: ...

    @abstractmethod
    def search_memory(
        self, namespace: str, query: str, top_k: int
    ) -> list[dict[str, Any]]: ...

    # --- 测试集(M10 / F-E.2/E.3:Badcase 入库 + 每周回归)---
    @abstractmethod
    def save_testcase(self, case: dict[str, Any]) -> int: ...

    @abstractmethod
    def list_testcases(self, only_active: bool = True) -> list[dict[str, Any]]: ...

    @abstractmethod
    def has_testcase(self, dedup_key: str) -> bool: ...
