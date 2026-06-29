"""LoopController:业务回退计数(F-D.1 / F-B.7)。

与节点重试(retry_counters)独立,不共用计数器(PRD 明确两类重试分离)。
质量不达标触发回退,按 loop key 累加;达 MAX_LOOP_ITERATIONS 不再回退,
置 need_decision 等 Host。
"""
from __future__ import annotations

from backend.config import settings
from backend.schema import CompanyState


class LoopController:
    def __init__(self, max_iterations: int | None = None) -> None:
        self._max = max_iterations or settings.max_loop_iterations

    def register_reject(self, state: CompanyState, loop_key: str) -> int:
        """记一次回退,返回该 loop 当前累计次数。"""
        state.loop_counters[loop_key] = state.loop_counters.get(loop_key, 0) + 1
        return state.loop_counters[loop_key]

    def can_rework(self, state: CompanyState, loop_key: str) -> bool:
        """是否还允许继续回退(未达上限)。"""
        return state.loop_counters.get(loop_key, 0) < self._max

    def iterations(self, state: CompanyState, loop_key: str) -> int:
        return state.loop_counters.get(loop_key, 0)
