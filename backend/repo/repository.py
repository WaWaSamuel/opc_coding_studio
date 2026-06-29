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
