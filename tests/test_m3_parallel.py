"""M3 无依赖节点并行 + 确定性汇合(F-B.1 / M02)。

验证:
  - 前端 + 后端并行跑,两份产物都并回主 state
  - 结果顺序 = job 声明顺序(确定性,非完成顺序)
  - token 增量正确累加回主 state
  - 真并发:用 barrier 证明两分支确实重叠执行
  - 分支异常按序重抛(不静默吞)
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from backend.core.cost_guard import CostGuard
from backend.core.model_adapter.base import ModelAdapter
from backend.core.roles.registry import RoleRegistry
from backend.errors import ModelCallError
from backend.orchestrator.node_runner import NodeRunner
from backend.orchestrator.parallel import ParallelExecutor, ParallelJob
from backend.orchestrator.retry import RetryController
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState, InvokeResult


def _artifact_json(role: str, files: list[str]) -> str:
    return json.dumps({
        "role": role, "task_id": "x", "status": "done",
        "artifact": {"files": files, "summary": f"{role} done"},
        "handoff_notes": "", "issues": [], "open_questions": [], "data": {},
    }, ensure_ascii=False)


class RoleAwareAdapter(ModelAdapter):
    """按角色返回不同产物;可选 barrier 验证真并发。"""

    def __init__(self, barrier: threading.Barrier | None = None,
                 fail_role: str | None = None):
        self._barrier = barrier
        self._fail_role = fail_role

    def _role(self, messages) -> str:
        sys = messages[0]["content"]
        for r in ("frontend-engineer-agent", "backend-engineer-agent"):
            if r in sys:
                return r
        return "unknown"

    def invoke(self, messages, schema=None, tier="large") -> InvokeResult:
        role = self._role(messages)
        if self._barrier is not None:
            self._barrier.wait(timeout=5)  # 两分支必须同时到达才放行
        if role == self._fail_role:
            raise ModelCallError(f"{role} 持续失败")
        files = {"frontend-engineer-agent": ["index.tsx"],
                 "backend-engineer-agent": ["app.py"]}.get(role, ["x"])
        return InvokeResult(content=_artifact_json(role, files),
                            tokens_in=10, tokens_out=20, latency_ms=1)


@pytest.fixture()
def runner_factory(tmp_path):
    repo = SqliteRepo(str(tmp_path / "m3par.db"))
    registry = RoleRegistry()
    cost = CostGuard(repo)
    ckpt = CheckpointStore(repo)

    def make(adapter, retry=None):
        return NodeRunner(adapter, registry, repo, cost, ckpt,
                          retry=retry or RetryController(max_retry=1))

    yield make
    repo.close()


def test_parallel_join_collects_both(runner_factory):
    runner = runner_factory(RoleAwareAdapter())
    ex = ParallelExecutor(runner)
    state = CompanyState(task_id="par-1")
    jobs = [
        ParallelJob("frontend-engineer-agent", "做前端"),
        ParallelJob("backend-engineer-agent", "做后端"),
    ]
    results = ex.run(state, jobs)
    # 结果顺序 = job 声明顺序
    assert results[0].role == "frontend-engineer-agent"
    assert results[1].role == "backend-engineer-agent"
    assert results[0].artifact.files == ["index.tsx"]
    assert results[1].artifact.files == ["app.py"]
    # 两份产物都并回主 state
    roles = {a.role for a in state.artifacts}
    assert roles == {"frontend-engineer-agent", "backend-engineer-agent"}
    # token 累加(2 节点 × 30)
    assert state.task_tokens == 60
    assert "parallel_join" in state.transition


def test_parallel_actually_concurrent(runner_factory):
    """barrier 要求两分支同时到达才放行 → 串行会卡死,能过即证明真并发。"""
    barrier = threading.Barrier(2)
    runner = runner_factory(RoleAwareAdapter(barrier=barrier))
    ex = ParallelExecutor(runner, max_workers=2)
    state = CompanyState(task_id="par-2")
    jobs = [
        ParallelJob("frontend-engineer-agent", "做前端"),
        ParallelJob("backend-engineer-agent", "做后端"),
    ]
    start = time.monotonic()
    results = ex.run(state, jobs)
    assert len(results) == 2
    assert time.monotonic() - start < 5  # 没卡死 = 确实并发到达 barrier


def test_parallel_branch_error_propagates(runner_factory):
    runner = runner_factory(
        RoleAwareAdapter(fail_role="backend-engineer-agent"),
        retry=RetryController(max_retry=2),
    )
    ex = ParallelExecutor(runner)
    state = CompanyState(task_id="par-3")
    jobs = [
        ParallelJob("frontend-engineer-agent", "做前端"),
        ParallelJob("backend-engineer-agent", "做后端"),
    ]
    with pytest.raises(ModelCallError):
        ex.run(state, jobs)


def test_single_job_no_thread(runner_factory):
    runner = runner_factory(RoleAwareAdapter())
    ex = ParallelExecutor(runner)
    state = CompanyState(task_id="par-4")
    results = ex.run(state, [ParallelJob("backend-engineer-agent", "做后端")])
    assert len(results) == 1
    assert results[0].role == "backend-engineer-agent"
