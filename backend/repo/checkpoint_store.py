"""CheckpointStore:断点恢复(F-D.4)。

包裹 Repository,提供"每步落盘 / 按 task_id 恢复"的语义。
LangGraph Checkpointer 适配留 M2(装 StateGraph 时);M1 先手动落盘。
"""
from __future__ import annotations

from backend.repo.repository import Repository
from backend.schema import CompanyState


class CheckpointStore:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def save(self, state: CompanyState) -> None:
        self._repo.save_checkpoint(state)

    def restore(self, task_id: str) -> CompanyState | None:
        return self._repo.load_checkpoint(task_id)
