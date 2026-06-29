"""M3 节点重试 + 全局熔断常量(F-D.2 / M07)。

验证:
  - 瞬时失败(ModelCallError/SelfRepairExhausted)原地重试至成功
  - 重试达上限后抛出原异常(不静默吞)
  - CostLimitExceeded 不重试,直接透传(保命阀语义)
  - retry_counters 与 loop_counters 完全独立(两类重试不共用计数器)
  - NodeRunner 层:模型瞬时失败被节点重试救回,产出合法 Artifact
"""
from __future__ import annotations

import json

import pytest

from backend.core.cost_guard import CostGuard
from backend.core.model_adapter.base import ModelAdapter
from backend.core.roles.registry import RoleRegistry
from backend.errors import CostLimitExceeded, ModelCallError
from backend.orchestrator.node_runner import NodeRunner
from backend.orchestrator.retry import RetryController
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState, InvokeResult


def test_retry_recovers_after_transient_failures():
    state = CompanyState(task_id="r1")
    ctrl = RetryController(max_retry=3)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ModelCallError("ark 503")
        return "ok"

    assert ctrl.run(state, "node:x", fn) == "ok"
    assert calls["n"] == 3
    assert state.retry_counters["node:x"] == 2  # 两次失败计数


def test_retry_exhausts_and_raises():
    state = CompanyState(task_id="r2")
    ctrl = RetryController(max_retry=3)

    def always_fail():
        raise ModelCallError("永远 503")

    with pytest.raises(ModelCallError):
        ctrl.run(state, "node:y", always_fail)
    assert state.retry_counters["node:y"] == 3


def test_cost_limit_not_retried():
    """成本硬限是保命阀:即刻上抛,绝不重试继续烧 token。"""
    state = CompanyState(task_id="r3")
    ctrl = RetryController(max_retry=3)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise CostLimitExceeded("task", 100, 100)

    with pytest.raises(CostLimitExceeded):
        ctrl.run(state, "node:z", fn)
    assert calls["n"] == 1  # 只调一次,不重试
    assert "node:z" not in state.retry_counters


def test_retry_and_loop_counters_independent():
    """节点重试与业务回退两个计数器互不共用(PRD 硬约束)。"""
    state = CompanyState(task_id="r4")
    ctrl = RetryController(max_retry=2)
    state.loop_counters["build-quality"] = 2  # 业务回退已记 2

    def fail_once():
        if ctrl.attempts(state, "node:a") == 0:
            raise ModelCallError("一次性抖动")
        return "ok"

    ctrl.run(state, "node:a", fail_once)
    assert state.retry_counters["node:a"] == 1
    assert state.loop_counters["build-quality"] == 2  # 未被节点重试影响


# --- NodeRunner 集成:模型前两次 5xx,第三次成功 ---

class FlakyAdapter(ModelAdapter):
    def __init__(self, fail_times: int):
        self._fail_times = fail_times
        self.calls = 0

    def invoke(self, messages, schema=None, tier="large") -> InvokeResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise ModelCallError(f"ark 5xx #{self.calls}")
        content = json.dumps({
            "role": "backend-engineer-agent",
            "task_id": "x",
            "status": "done",
            "artifact": {"files": ["app.py"], "summary": "ok"},
            "handoff_notes": "",
            "issues": [],
            "open_questions": [],
            "data": {},
        }, ensure_ascii=False)
        return InvokeResult(content=content, tokens_in=10, tokens_out=20, latency_ms=1)


@pytest.fixture()
def runner_factory(tmp_path):
    repo = SqliteRepo(str(tmp_path / "m3retry.db"))
    registry = RoleRegistry()
    cost = CostGuard(repo)
    ckpt = CheckpointStore(repo)

    def make(adapter, retry=None):
        return NodeRunner(adapter, registry, repo, cost, ckpt, retry=retry)

    yield make
    repo.close()


def test_node_runner_recovers_from_model_5xx(runner_factory):
    adapter = FlakyAdapter(fail_times=2)
    runner = runner_factory(adapter, retry=RetryController(max_retry=3))
    state = CompanyState(task_id="m3-node-retry")
    art = runner.run(state, "backend-engineer-agent", "实现接口")
    assert art.status.value == "done"
    assert adapter.calls == 3  # 两次失败 + 一次成功
    assert state.retry_counters.get("node:backend-engineer-agent") == 2


def test_node_runner_gives_up_after_max_retry(runner_factory):
    adapter = FlakyAdapter(fail_times=99)
    runner = runner_factory(adapter, retry=RetryController(max_retry=3))
    state = CompanyState(task_id="m3-node-giveup")
    with pytest.raises(ModelCallError):
        runner.run(state, "backend-engineer-agent", "实现接口")
    assert adapter.calls == 3
