"""M2 RuntimeGraph 串行编排种子测试(电商业务流 PoC)。

用一个"角色感知"的脚本化 Adapter,离线、确定性地覆盖:
  - happy path:CEO 路由 → 部长拆解 → 执行 → Loop(规则+语义)→ 验收 → 汇总 → done
  - 规则先行 reject:硬约束不过时不进模型(loop-judge 不被调用)
  - 业务回退后修复通过(LoopController 计数)
  - 回退达上限 → need_decision 等 Host
  - CEO 路由结果传播
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from backend.core.cost_guard import CostGuard
from backend.core.model_adapter.base import ModelAdapter
from backend.core.roles.registry import RoleRegistry
from backend.orchestrator.graph_runtime import RuntimeGraph
from backend.orchestrator.loop import LoopController
from backend.orchestrator.node_runner import NodeRunner
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState, InvokeResult, TaskStatus

GOAL = "搭建一个最小电商下单接口:商品列表 + 下单 + 订单查询。"
# 验收标准(语义判定角色逐条核对用;M2 不做机械子串命中)
ACCEPTANCE = ["提供商品列表接口", "提供下单与订单查询接口"]


def _which_role(messages: list[dict[str, str]]) -> str:
    system = messages[0]["content"]
    for rid in (
        "ceo-orchestrator-agent",
        "dev-lead-agent",
        "loop-judge-agent",
        "qa-acceptance-agent",
        "backend-engineer-agent",
        "frontend-engineer-agent",
    ):
        if rid in system:
            return rid
    return "unknown"


class ScriptedAdapter(ModelAdapter):
    """角色感知脚本化模型。build_results 控制每轮 build 的好坏。"""

    def __init__(self, build_results: list[str], judge_verdict: str = "pass"):
        # build_results[i] ∈ {"good","empty"}:第 i 轮 build 的产物质量
        self._build_results = build_results
        self._judge_verdict = judge_verdict
        self.calls: dict[str, int] = {}

    def _bump(self, role: str) -> int:
        self.calls[role] = self.calls.get(role, 0) + 1
        return self.calls[role]

    def invoke(self, messages, schema=None, tier="large") -> InvokeResult:
        role = _which_role(messages)
        n = self._bump(role)
        content = self._content_for(role, n, messages)
        return InvokeResult(content=content, tokens_in=50, tokens_out=80, latency_ms=1)

    def _content_for(self, role: str, n: int, messages) -> str:
        if role == "ceo-orchestrator-agent":
            return _j(role, data={"department": "engineering", "is_major": False,
                                  "reason": "常规交付"})
        if role == "dev-lead-agent":
            user = messages[1]["content"]
            if "最终汇总" in user:
                return _j(role, summary="电商下单接口交付完成",
                          files=["app.py"], data={})
            return _j(role, data={
                "todo_plan": [
                    {"id": "T1", "desc": "商品列表接口", "owner_role":
                     "backend-engineer-agent", "status": "todo"},
                    {"id": "T2", "desc": "下单+订单查询", "owner_role":
                     "backend-engineer-agent", "status": "todo"},
                ],
                "acceptance": ACCEPTANCE,
            })
        if role == "backend-engineer-agent":
            # 第 n 次 build 取 build_results[n-1],越界用最后一个
            idx = min(n - 1, len(self._build_results) - 1)
            kind = self._build_results[idx]
            if kind == "empty":
                return _j(role, status="need_rework", files=[],
                          summary="尚未完成", data={})
            return _j(role, files=["app.py", "order_api.py"],
                      summary="实现 app.py 含 order 下单与订单查询接口",
                      data={})
        if role == "loop-judge-agent":
            return _j(role, data={"verdict": self._judge_verdict,
                                  "failed_checks": [], "reason": "语义合理",
                                  "suggestion": ""})
        if role == "qa-acceptance-agent":
            return _j(role, data={"verdict": "pass", "checked": [
                {"item": a, "passed": True} for a in ACCEPTANCE]})
        return _j(role)


def _j(role: str, *, status: str = "done", files=None, summary: str = "",
       data: dict[str, Any] | None = None) -> str:
    return json.dumps({
        "role": role,
        "task_id": "scripted",
        "status": status,
        "artifact": {"files": files or [], "summary": summary},
        "handoff_notes": "",
        "issues": [],
        "open_questions": [],
        "data": data or {},
    }, ensure_ascii=False)


@pytest.fixture()
def graph_factory(tmp_path):
    repo = SqliteRepo(str(tmp_path / "m2.db"))
    registry = RoleRegistry()
    cost = CostGuard(repo)
    ckpt = CheckpointStore(repo)

    def make(adapter, loop=None):
        runner = NodeRunner(adapter, registry, repo, cost, ckpt)
        return RuntimeGraph(runner, loop=loop or LoopController())

    yield make
    repo.close()


def test_happy_path_end_to_end(graph_factory):
    graph = graph_factory(ScriptedAdapter(build_results=["good"]))
    state = CompanyState(task_id="m2-happy")
    res = graph.run(state, GOAL)

    assert res.status == TaskStatus.DONE
    assert state.status == TaskStatus.DONE
    assert state.todo_plan and len(state.todo_plan) == 2
    # 全链路角色都被调用过
    events = [e["event"] for e in res.events]
    assert "ceo_route" in events
    assert "dev_plan" in events
    assert "acceptance" in events
    assert "graph_done" in events


def test_ceo_route_propagates(graph_factory):
    graph = graph_factory(ScriptedAdapter(build_results=["good"]))
    state = CompanyState(task_id="m2-route")
    res = graph.run(state, GOAL)
    route = next(e for e in res.events if e["event"] == "ceo_route")
    assert route["department"] == "engineering"
    assert route["is_major"] is False


def test_rule_check_first_blocks_model_judge(graph_factory):
    """硬约束(空文件)不过 → 直接 reject,loop-judge 不应被调用。"""
    adapter = ScriptedAdapter(build_results=["empty", "good"])
    graph = graph_factory(adapter)
    state = CompanyState(task_id="m2-rule")
    res = graph.run(state, GOAL)

    assert res.status == TaskStatus.DONE  # 第二轮修复后通过
    # 第一轮规则不过,loop-judge 只在第二轮(规则过)被调用一次
    assert adapter.calls.get("loop-judge-agent", 0) == 1
    assert adapter.calls.get("backend-engineer-agent") == 2
    # 有一次 rework 事件
    assert any(e["event"] == "rework" for e in res.events)
    assert state.loop_counters.get("build-quality") == 1


def test_loop_exhausted_need_decision(graph_factory):
    """build 始终空文件 → 规则永不过 → 回退达上限 → need_decision。"""
    adapter = ScriptedAdapter(build_results=["empty"])
    graph = graph_factory(adapter, loop=LoopController(max_iterations=3))
    state = CompanyState(task_id="m2-exhaust")
    res = graph.run(state, GOAL)

    assert res.status == TaskStatus.NEED_DECISION
    assert state.status == TaskStatus.NEED_DECISION
    assert state.loop_counters.get("build-quality") == 3
    # loop-judge 从未被调用(规则先行全程拦截)
    assert adapter.calls.get("loop-judge-agent", 0) == 0
    assert any(e["event"] == "need_decision" for e in res.events)


def test_model_judge_reject_then_pass(graph_factory):
    """规则过但模型语义 reject 一次,回退后再判 pass。"""

    class FlakyJudge(ScriptedAdapter):
        def _content_for(self, role, n, messages):
            if role == "loop-judge-agent" and n == 1:
                return _j(role, data={"verdict": "reject",
                                      "failed_checks": ["体验不满足宿主意图"],
                                      "reason": "首版体验差", "suggestion": "改进交互"})
            return super()._content_for(role, n, messages)

    adapter = FlakyJudge(build_results=["good"])
    graph = graph_factory(adapter)
    state = CompanyState(task_id="m2-judge")
    res = graph.run(state, GOAL)

    assert res.status == TaskStatus.DONE
    assert adapter.calls.get("loop-judge-agent") == 2
    assert state.loop_counters.get("build-quality") == 1
