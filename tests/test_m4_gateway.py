"""M4 入口层 + 事件总线 + 人在环 种子测试。

覆盖:
  - EventBus:pub/sub、persist 开关、done 哨兵收流
  - Gateway:classify_intent、SessionRouter 双向映射、HostAuthorizer 身份校验
  - DecisionGate:submit/wait/超时
  - OrchestratorService:submit→后台跑→事件流→done、cost 聚合、非 Host 拒绝
  - FastAPI:/health、/command、/events(SSE)、/decision、/task、/cost
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from backend.core.event_bus import Event, EventBus
from backend.core.model_adapter.base import ModelAdapter
from backend.gateway.host_command import HostCommand, classify_intent
from backend.gateway.session_router import HostAuthorizer, SessionRouter
from backend.orchestrator.decision_gate import Decision, DecisionGate
from backend.orchestrator.service import OrchestratorService
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import InvokeResult, TaskStatus

GOAL = "搭建一个最小电商下单接口:商品列表 + 下单 + 订单查询。"
ACCEPTANCE = ["提供商品列表接口", "提供下单与订单查询接口"]


# ── 角色感知脚本化 Adapter(离线、确定性)─────────────────────
def _which_role(messages: list[dict[str, str]]) -> str:
    system = messages[0]["content"]
    for rid in (
        "ceo-orchestrator-agent", "pm-prd-agent", "dev-lead-agent",
        "loop-judge-agent", "qa-acceptance-agent",
        "backend-engineer-agent", "frontend-engineer-agent",
    ):
        if rid in system:
            return rid
    return "unknown"


def _j(role: str, *, status: str = "done", files=None, summary: str = "",
       data: dict[str, Any] | None = None) -> str:
    return json.dumps({
        "role": role, "task_id": "scripted", "status": status,
        "artifact": {"files": files or [], "summary": summary},
        "handoff_notes": "", "issues": [], "open_questions": [],
        "data": data or {},
    }, ensure_ascii=False)


class ScriptedAdapter(ModelAdapter):
    """happy-path:CEO→部长→后端→判定 pass→验收 pass→汇总→done。"""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def invoke(self, messages, schema=None, tier="large") -> InvokeResult:
        role = _which_role(messages)
        self.calls[role] = self.calls.get(role, 0) + 1
        content = self._content_for(role, messages)
        return InvokeResult(content=content, tokens_in=50, tokens_out=80, latency_ms=1)

    def _content_for(self, role: str, messages) -> str:
        if role == "ceo-orchestrator-agent":
            return _j(role, data={"department": "engineering", "is_major": False,
                                  "reason": "常规交付"})
        if role == "dev-lead-agent":
            if "最终汇总" in messages[1]["content"]:
                return _j(role, summary="电商下单接口交付完成", files=["app.py"], data={})
            return _j(role, data={
                "todo_plan": [
                    {"id": "T1", "desc": "商品列表接口",
                     "owner_role": "backend-engineer-agent", "status": "todo"},
                ],
                "acceptance": ACCEPTANCE,
            })
        if role == "backend-engineer-agent":
            return _j(role, files=["app.py", "order_api.py"],
                      summary="实现下单与订单查询接口", data={})
        if role == "loop-judge-agent":
            return _j(role, data={"verdict": "pass", "failed_checks": [],
                                  "reason": "语义合理", "suggestion": ""})
        if role == "qa-acceptance-agent":
            return _j(role, data={"verdict": "pass",
                                  "checked": [{"item": a, "passed": True}
                                              for a in ACCEPTANCE]})
        return _j(role)


# ── EventBus ─────────────────────────────────────────────────
def test_event_bus_pubsub_and_sentinel():
    bus = EventBus(repo=None)
    q = bus.subscribe("t1")
    bus.emit("t1", "graph_start", role="orchestrator", persist=False)
    bus.emit("t1", "done", role="orchestrator", persist=False)
    first = q.get(timeout=1)
    assert first["event"] == "graph_start"
    second = q.get(timeout=1)
    assert second["event"] == "done"
    sentinel = q.get(timeout=1)
    assert EventBus.is_sentinel(sentinel)


def test_event_bus_persist_toggle(tmp_path):
    repo = SqliteRepo(str(tmp_path / "bus.db"))
    bus = EventBus(repo)
    bus.emit("t2", "graph_start", role="r", persist=True)
    bus.emit("t2", "thinking", role="r", persist=False)
    logs = repo.logs_for("t2")
    events = [e["event"] for e in logs]
    assert "graph_start" in events
    assert "thinking" not in events  # persist=False 不落库
    repo.close()


# ── Gateway ──────────────────────────────────────────────────
def test_classify_intent():
    assert classify_intent("帮我搭一个下单接口") == "runtime"
    assert classify_intent("请改工作流让流程更顺") == "edit"
    assert classify_intent("自迭代优化一下角色提示词") == "edit"


def test_session_router_roundtrip():
    sr = SessionRouter()
    tid = sr.new_task_id("chat-1", prefix="task")
    assert tid.startswith("task-")
    assert sr.task_for("chat-1") == tid
    assert sr.session_for(tid) == "chat-1"


def test_host_authorizer():
    auth = HostAuthorizer("ou_host")
    assert auth.verify_lark("ou_host") is True
    assert auth.verify_lark("ou_other") is False
    # 空白名单 → 不限制(本地调试)
    assert HostAuthorizer("").verify_lark("anyone") is True
    assert auth.verify_web() is True


# ── DecisionGate ─────────────────────────────────────────────
def test_decision_gate_submit_and_wait():
    gate = DecisionGate()
    import threading
    result: dict[str, Any] = {}

    def waiter():
        result["d"] = gate.wait("tk", timeout=2.0)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    assert gate.is_waiting("tk")
    assert gate.submit("tk", Decision(verdict="pass", reason="ok")) is True
    t.join(timeout=2)
    assert result["d"] is not None
    assert result["d"].verdict == "pass"


def test_decision_gate_timeout():
    gate = DecisionGate()
    assert gate.wait("none", timeout=0.2) is None


# ── OrchestratorService ──────────────────────────────────────
@pytest.fixture()
def service(tmp_path):
    svc = OrchestratorService(db_path=str(tmp_path / "svc.db"),
                              adapter=ScriptedAdapter())
    yield svc
    svc.close()


def _drain_until_done(svc: OrchestratorService, task_id: str, timeout=10.0):
    q = svc.subscribe(task_id)
    events = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                item = q.get(True, 1.0)
            except Exception:
                continue
            if EventBus.is_sentinel(item):
                break
            events.append(item)
    finally:
        svc.unsubscribe(task_id, q)
    return events


def test_service_submit_runs_to_done(service):
    cmd = HostCommand(channel="web", session_id="s1", text=GOAL,
                      host_verified=True, intent="runtime")
    task_id = service.submit(cmd)
    events = _drain_until_done(service, task_id)
    types = [e["event"] for e in events]
    assert "done" in types
    # 等线程落库收口
    service._threads[task_id].join(timeout=5)
    assert service.task_status(task_id) == TaskStatus.DONE.value
    cost = service.cost(task_id)
    assert cost["total_tokens"] > 0


def test_service_rejects_non_host(service):
    cmd = HostCommand(channel="lark", session_id="s2", text=GOAL,
                      host_verified=False)
    with pytest.raises(PermissionError):
        service.submit(cmd)


# ── FastAPI ──────────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from backend.api.app import create_app
    svc = OrchestratorService(db_path=str(tmp_path / "api.db"),
                              adapter=ScriptedAdapter())
    app = create_app(svc)
    with TestClient(app) as c:
        yield c, svc
    svc.close()


def test_api_health(client):
    c, _ = client
    assert c.get("/health").json() == {"status": "ok"}


def test_api_command_and_task(client):
    c, svc = client
    resp = c.post("/command", json={"text": GOAL, "session_id": "web-1"})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert resp.json()["intent"] == "runtime"
    # 等编排线程跑完
    svc._threads[task_id].join(timeout=10)
    snap = c.get(f"/task/{task_id}")
    assert snap.status_code == 200
    assert snap.json()["task_id"] == task_id
    cost = c.get("/cost", params={"task_id": task_id})
    assert cost.json()["total_tokens"] > 0


def test_api_events_sse(client):
    c, svc = client
    task_id = c.post("/command", json={"text": GOAL}).json()["task_id"]
    svc._threads[task_id].join(timeout=10)
    # 任务已结束,/events 先补发历史事件,done 后收流
    with c.stream("GET", "/events", params={"task_id": task_id}) as resp:
        seen = []
        for line in resp.iter_lines():
            if line and line.startswith("event:"):
                seen.append(line.split(":", 1)[1].strip())
            if "done" in seen:
                break
    assert "done" in seen


def test_api_task_not_found(client):
    c, _ = client
    assert c.get("/task/does-not-exist").status_code == 404
