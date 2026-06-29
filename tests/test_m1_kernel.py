"""M1 内核种子测试(对应 PRD G7 种子用例冷启动的雏形)。

覆盖:happy path / JSON 自修复 / 成本硬熔断 / 断点恢复。
全部用 MockAdapter,离线、确定性。
"""
from __future__ import annotations

import pytest

from backend.core.cost_guard import CostGuard
from backend.core.model_adapter.mock_adapter import MockAdapter
from backend.core.roles.registry import RoleRegistry
from backend.errors import CostLimitExceeded, SelfRepairExhausted
from backend.orchestrator.node_runner import NodeRunner
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import ArtifactStatus, CompanyState


@pytest.fixture()
def env(tmp_path):
    repo = SqliteRepo(str(tmp_path / "test.db"))
    registry = RoleRegistry()
    cost = CostGuard(repo)
    ckpt = CheckpointStore(repo)
    yield {"repo": repo, "registry": registry, "cost": cost, "ckpt": ckpt}
    repo.close()


def _runner(env, adapter):
    return NodeRunner(adapter, env["registry"], env["repo"], env["cost"], env["ckpt"])


def test_happy_path_produces_valid_artifact(env):
    runner = _runner(env, MockAdapter())
    state = CompanyState(task_id="t-happy")
    art = runner.run(state, "backend-engineer-agent", "做点事")

    assert art.status == ArtifactStatus.DONE
    assert art.task_id == "t-happy"
    assert art.role == "backend-engineer-agent"
    assert state.task_tokens == 300  # 100 in + 200 out


def test_self_repair_recovers_from_bad_json(env):
    """前一次返回坏 JSON,第二次返回合法 Artifact → 自修复成功。"""
    calls = {"n": 0}
    good = MockAdapter()._responder([])  # 复用默认合法 JSON

    def responder(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return "这不是 JSON,故意坏的输出"
        return good

    runner = _runner(env, MockAdapter(responder=responder))
    state = CompanyState(task_id="t-repair")
    art = runner.run(state, "backend-engineer-agent", "做点事")

    assert art.status == ArtifactStatus.DONE
    assert calls["n"] == 2  # 一次坏 + 一次修复
    assert state.retry_counters.get("self_repair") == 1


def test_self_repair_exhausted_raises(env):
    """始终坏 JSON → 自修复耗尽抛 SelfRepairExhausted。"""
    runner = _runner(env, MockAdapter(responder=lambda _m: "永远坏的输出"))
    state = CompanyState(task_id="t-exhaust")
    with pytest.raises(SelfRepairExhausted):
        runner.run(state, "backend-engineer-agent", "做点事")


def test_hard_cost_limit_trips(env, monkeypatch):
    """单次调用 token 超过硬限 → CostLimitExceeded(F-D.6)。"""
    from backend.config import settings

    monkeypatch.setattr(settings, "hard_task_tokens", 250)
    # MockAdapter 默认 300 token > 250 硬限
    runner = _runner(env, MockAdapter())
    state = CompanyState(task_id="t-cost")
    with pytest.raises(CostLimitExceeded) as ei:
        runner.run(state, "backend-engineer-agent", "做点事")
    assert ei.value.scope == "task"


def test_checkpoint_restore(env):
    runner = _runner(env, MockAdapter())
    state = CompanyState(task_id="t-ckpt")
    runner.run(state, "backend-engineer-agent", "做点事")

    restored = env["ckpt"].restore("t-ckpt")
    assert restored is not None
    assert len(restored.artifacts) == 1
    assert restored.transition.endswith("done")
    assert restored.task_tokens == 300


def test_daily_tokens_accumulate(env):
    runner = _runner(env, MockAdapter())
    runner.run(CompanyState(task_id="t1"), "backend-engineer-agent", "a")
    runner.run(CompanyState(task_id="t2"), "backend-engineer-agent", "b")
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert env["repo"].get_daily_tokens(day) == 600
