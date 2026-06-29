"""无依赖节点并行执行 + 确定性汇合(F-B.1 / M02)。

PRD M02 边界:并行节点(前后端)需在汇合节点 join。

为什么要子状态隔离:NodeRunner 会就地改 CompanyState(追加 artifacts、
累加 task_tokens、写 retry_counters / current_role / transition)。多线程
同时改一个 state 会产生竞态。做法:

  ① fork:给每个分支一份 state 深拷贝(隔离),分支内 NodeRunner 自由改;
  ② run:ThreadPoolExecutor 并行跑各分支(IO 密集——等模型,线程足够);
  ③ join:**按 job 声明顺序**(非完成顺序)确定性合并回主 state——
     追加各分支新增的 artifacts、累加 token 增量、合并 retry 计数增量。

这样结果与"串行依次跑"等价且可复现,只是把等待时间重叠掉了。
分支内若抛异常(重试耗尽/成本熔断),join 阶段按顺序重新抛出,不吞错。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from backend.orchestrator.node_runner import NodeRunner
from backend.schema import Artifact, CompanyState


@dataclass
class ParallelJob:
    role_id: str
    task_text: str
    upstream: Artifact | None = None


@dataclass
class _BranchOutcome:
    artifacts: list[Artifact]
    token_delta: int
    retry_delta: dict[str, int]
    error: Exception | None = None


class ParallelExecutor:
    def __init__(self, runner: NodeRunner, max_workers: int = 4) -> None:
        self._runner = runner
        self._max_workers = max_workers

    def run(self, state: CompanyState, jobs: list[ParallelJob]) -> list[Artifact]:
        """并行跑无依赖 jobs,确定性汇合回 state,返回与 jobs 同序的 artifacts。"""
        if not jobs:
            return []
        if len(jobs) == 1:  # 单任务无需起线程
            j = jobs[0]
            return [self._runner.run(state, j.role_id, j.task_text, j.upstream)]

        base_tokens = state.task_tokens
        base_retry = dict(state.retry_counters)

        def _branch(job: ParallelJob) -> _BranchOutcome:
            sub = state.model_copy(deep=True)
            before = len(sub.artifacts)
            try:
                self._runner.run(sub, job.role_id, job.task_text, job.upstream)
            except Exception as exc:  # 重试耗尽/成本熔断等,join 时按序重抛
                return _BranchOutcome([], 0, {}, error=exc)
            new_arts = sub.artifacts[before:]
            token_delta = sub.task_tokens - base_tokens
            retry_delta = {
                k: v - base_retry.get(k, 0)
                for k, v in sub.retry_counters.items()
                if v - base_retry.get(k, 0) > 0
            }
            return _BranchOutcome(new_arts, token_delta, retry_delta)

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            outcomes = list(pool.map(_branch, jobs))  # 保持与 jobs 同序

        # 确定性 join:按 job 声明顺序合并
        results: list[Artifact] = []
        for job, out in zip(jobs, outcomes):
            if out.error is not None:
                raise out.error
            state.task_tokens += out.token_delta
            for k, d in out.retry_delta.items():
                state.retry_counters[k] = state.retry_counters.get(k, 0) + d
            for art in out.artifacts:
                state.artifacts.append(art)
            results.append(out.artifacts[-1] if out.artifacts else None)

        state.transition = (
            "parallel_join("
            + ",".join(j.role_id for j in jobs)
            + ")"
        )
        self._runner.checkpoint(state)
        return results
