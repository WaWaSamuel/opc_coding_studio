"""CostGuard:成本熔断(F-D.6,一人公司保命阀)。

每次模型调用后累加 token:
  - 任务累计 >= SOFT_TASK_TOKENS  → 软限:告警 + 建议降档(返回 should_downgrade)
  - 任务累计 >= HARD_TASK_TOKENS  → 硬限:抛 CostLimitExceeded,即刻终止任务报 Host
  - 当日全局累计 >= MAX_DAILY_TOKENS → 抛 CostLimitExceeded,熔断当日新增

对应 M07 全局熔断常量表:SOFT/HARD_TASK_TOKENS / MAX_DAILY_TOKENS。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.config import settings
from backend.errors import CostLimitExceeded
from backend.repo.repository import Repository
from backend.schema import CompanyState


@dataclass
class CostCheck:
    soft_breached: bool
    should_downgrade: bool  # 软限触发后建议路由小模型
    task_tokens: int
    daily_tokens: int


class CostGuard:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def charge(self, state: CompanyState, tokens: int) -> CostCheck:
        """记账并执行熔断判定。硬限/日限触发直接抛异常。"""
        state.task_tokens += tokens
        daily = self._repo.add_daily_tokens(self._today(), tokens)

        # 硬限:任务级
        if state.task_tokens >= settings.hard_task_tokens:
            raise CostLimitExceeded("task", state.task_tokens, settings.hard_task_tokens)
        # 硬限:全局日级
        if daily >= settings.max_daily_tokens:
            raise CostLimitExceeded("daily", daily, settings.max_daily_tokens)

        soft = state.task_tokens >= settings.soft_task_tokens
        return CostCheck(
            soft_breached=soft,
            should_downgrade=soft,
            task_tokens=state.task_tokens,
            daily_tokens=daily,
        )
