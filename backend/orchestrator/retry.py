"""RetryController:节点重试(F-D.2 / M07)。

与业务回退(LoopController/loop_counters)**完全独立**——这是 PRD 硬约束:
两个计数器互不共用,节点重试到上限**不自动转**业务回退。

语义区分:
  - 业务回退(LoopController):质量不达标,跨节点重做,产物语义层面的失败。
  - 节点重试(RetryController):瞬时/技术性失败(模型 5xx、限流、超时、
    JSON 解析自修复仍失败),在**原地**对同一节点重试,不改变图的流转。

熔断:节点重试达 MAX_NODE_RETRY 仍失败 → 不再重试,向上抛出由编排层
置 error / need_decision(每条自动恢复路径都有对应熔断上限,借 Claude Code)。

注意:成本熔断 CostLimitExceeded 属于"保命阀"语义,**不在此重试**——
触硬限要即刻终止任务报 Host,重试只会继续烧 token。调用方据此区分。
"""
from __future__ import annotations

from typing import Callable, TypeVar

from backend.config import settings
from backend.errors import CostLimitExceeded, ModelCallError, SelfRepairExhausted
from backend.schema import CompanyState

T = TypeVar("T")

# 视为"瞬时/技术性失败,可原地重试"的异常类型。
# CostLimitExceeded 明确排除:触限即终止,不重试。
RETRIABLE = (ModelCallError, SelfRepairExhausted)


class RetryController:
    def __init__(self, max_retry: int | None = None) -> None:
        self._max = max_retry if max_retry is not None else settings.max_node_retry

    def run(
        self,
        state: CompanyState,
        retry_key: str,
        fn: Callable[[], T],
        on_retry: Callable[[int, Exception], None] | None = None,
    ) -> T:
        """对 fn 做节点级重试(原地)。

        retry_key 用于在 state.retry_counters 累计(与 loop_counters 独立)。
        成功返回结果;瞬时失败重试至上限后抛出原异常;
        CostLimitExceeded 直接透传(不重试,交保命阀处理)。
        """
        last_err: Exception | None = None
        for attempt in range(self._max):
            try:
                return fn()
            except CostLimitExceeded:
                raise  # 保命阀:即刻上抛,不重试
            except RETRIABLE as exc:
                last_err = exc
                state.retry_counters[retry_key] = (
                    state.retry_counters.get(retry_key, 0) + 1
                )
                if on_retry is not None:
                    on_retry(state.retry_counters[retry_key], exc)
        assert last_err is not None
        raise last_err

    def attempts(self, state: CompanyState, retry_key: str) -> int:
        return state.retry_counters.get(retry_key, 0)
