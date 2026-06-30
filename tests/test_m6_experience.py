"""M6 体验增强 + 自改代码 种子测试。

覆盖:
  - F-A.1/E.7 意图初判扩词:改 web/样式/颜色 → edit
  - F-A.7 对话式决策解析 classify_decision
  - F-E.7 写路径白/黑名单 check_write_path / filter_writable
  - F-A.8 多工作流全景 edit_graph(workflow=edit|runtime)+ 角色详情 role_detail
  - F-E.6 自重启闸门关 → restart_required dry-run + EditGraph 收口信号
  - F-A.10 多模态图片上传 /upload(类型/大小校验 + 防穿越)
  - API:/role/{id}、/edit/restart、/edit/graph?workflow=、/upload
"""
from __future__ import annotations

import pytest

from backend.gateway.host_command import classify_decision, classify_intent
from backend.orchestrator.graph_edit import _restart_scope
from backend.orchestrator.tools import (
    check_write_path,
    filter_writable,
)
from backend.services.restarter import RestartResult, ServiceRestarter


# ── F-A.1 / F-E.7 意图初判扩词 ───────────────────────────────
@pytest.mark.parametrize("text", [
    "修改web style颜色为粉色", "改样式", "把按钮改颜色", "改前端布局",
    "改后端接口", "改 UI 主题", "改代码",
])
def test_classify_intent_edit_hits(text):
    assert classify_intent(text) == "edit"


@pytest.mark.parametrize("text", [
    "帮我做个电商落地页", "写一篇周报", "生成一份销售数据分析",
])
def test_classify_intent_runtime_default(text):
    assert classify_intent(text) == "runtime"


# ── F-A.7 对话式决策解析 ─────────────────────────────────────
def test_classify_decision_verdicts():
    assert classify_decision("可以,放行吧") == "pass"
    assert classify_decision("打回返工") == "reject"
    assert classify_decision("终止任务") == "abort"
    # abort/reject 优先级高于 pass:避免"不通过"被误判
    assert classify_decision("这个不行,重做") == "reject"
    # 无法判定 → None,交界面按钮兜底
    assert classify_decision("嗯嗯随便") is None
    assert classify_decision("") is None


# ── F-E.7 写路径白/黑名单 ────────────────────────────────────
def test_check_write_path_allow():
    assert check_write_path("backend/api/app.py") is None
    assert check_write_path("frontend/src/App.tsx") is None
    assert check_write_path("scripts/opc.sh") is None


def test_check_write_path_deny():
    # 黑名单目录:运行态数据 / 版本元数据 / 依赖产物
    assert check_write_path("data/app.db") is not None
    assert check_write_path(".git/config") is not None
    assert check_write_path("frontend/node_modules/x.js") is not None
    # 黑名单文件:密钥
    assert check_write_path(".env") is not None
    assert check_write_path("backend/.env") is not None
    # 越界:绝对路径 / ../ 穿越
    assert check_write_path("/etc/passwd") is not None
    assert check_write_path("../../etc/passwd") is not None
    # 不在白名单内的根级文件
    assert check_write_path("hack.py") is not None


def test_filter_writable_splits():
    allowed, denied = filter_writable({
        "backend/x.py": "a",
        "data/db.sqlite": "b",
        "../escape.py": "c",
    })
    assert "backend/x.py" in allowed
    assert "data/db.sqlite" in denied and "../escape.py" in denied


# ── F-E.6 重启范围判断 + 闸门关 dry-run ──────────────────────
def test_restart_scope():
    assert _restart_scope(["backend/api/app.py"]) == "backend"
    assert _restart_scope(["frontend/src/App.tsx"]) == "frontend"
    assert _restart_scope(["backend/x.py", "frontend/src/y.tsx"]) == "both"
    # backend/ 下的角色 YAML 仍归类 backend(进程内 registry 需重启重载)
    assert _restart_scope(["backend/core/roles/specs/x.yaml"]) == "backend"
    assert _restart_scope([]) is None


def test_restarter_gate_closed_dry_run():
    r = ServiceRestarter(enabled=False)
    assert r.enabled is False
    res = r.restart("both")
    assert isinstance(res, RestartResult)
    assert res.dry_run is True and res.ok is True
    assert "restart_required" in res.note


def test_restarter_unknown_scope():
    res = ServiceRestarter(enabled=False).restart("nope")  # type: ignore[arg-type]
    assert res.dry_run is True  # 闸门关优先,先回信号


# ── F-A.8 多工作流全景 + 角色详情(service 层)────────────────
def _svc(tmp_path):
    from backend.orchestrator.service import OrchestratorService
    return OrchestratorService(db_path=str(tmp_path / "m6.db"))


def test_edit_graph_workflow_switch(tmp_path):
    svc = _svc(tmp_path)
    try:
        edit_spec = svc.edit_graph(workflow="edit")
        assert edit_spec["workflow"] == "edit"
        assert any(n["id"] == "edit-engineer-agent" for n in edit_spec["nodes"])
        rt_spec = svc.edit_graph(workflow="runtime")
        assert rt_spec["workflow"] == "runtime"
        assert any(n["id"] == "backend-engineer-agent" for n in rt_spec["nodes"])
        # 两图都带 workflows 清单(供前端切换)+ git 状态
        for spec in (edit_spec, rt_spec):
            ids = {w["id"] for w in spec["workflows"]}
            assert ids == {"edit", "runtime"}
            assert "git" in spec
        # role 节点带 role_id 供下钻
        assert all("role_id" in n for n in edit_spec["nodes"]
                   if n["kind"] in ("role", "gate"))
    finally:
        svc.close()


def test_role_detail(tmp_path):
    svc = _svc(tmp_path)
    try:
        detail = svc.role_detail("edit-engineer-agent")
        assert detail is not None
        assert detail["role_id"] == "edit-engineer-agent"
        assert detail["model_tier"]
        assert detail["responsibility"]
        # edit-engineer-agent 在白名单里能调 fs.write/git.*
        assert any(t.startswith("git.") or t.startswith("fs.")
                   for t in detail["tools"])
        assert svc.role_detail("no-such-role") is None
    finally:
        svc.close()


def test_restart_service_gate_closed(tmp_path):
    svc = _svc(tmp_path)
    try:
        res = svc.restart_service("backend")
        assert res["dry_run"] is True
        assert "restart_required" in res["note"]
    finally:
        svc.close()


# ── API:新增路由 ────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from backend.api.app import create_app
    from backend.orchestrator.service import OrchestratorService
    svc = OrchestratorService(db_path=str(tmp_path / "api6.db"))
    app = create_app(svc)
    with TestClient(app) as c:
        yield c, svc
    svc.close()


def test_api_edit_graph_workflow(client):
    c, _ = client
    r = c.get("/edit/graph", params={"workflow": "runtime"})
    assert r.status_code == 200
    body = r.json()
    assert body["workflow"] == "runtime"
    assert any(n["id"] == "backend-engineer-agent" for n in body["nodes"])


def test_api_role_detail(client):
    c, _ = client
    r = c.get("/role/edit-lead-agent")
    assert r.status_code == 200
    assert r.json()["role_id"] == "edit-lead-agent"
    assert c.get("/role/ghost").status_code == 404


def test_api_edit_restart_gate_closed(client):
    c, _ = client
    r = c.post("/edit/restart", json={"scope": "backend"})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert "restart_required" in body["note"]


def test_api_upload_image_roundtrip(client):
    c, _ = client
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    r = c.post("/upload", files={"file": ("x.png", png, "image/png")})
    assert r.status_code == 200
    body = r.json()
    assert body["url"].startswith("/uploads/")
    assert body["content_type"] == "image/png"
    # 回访
    got = c.get(body["url"])
    assert got.status_code == 200
    assert got.content == png


def test_api_upload_rejects_non_image(client):
    c, _ = client
    r = c.post("/upload", files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 415


def test_api_upload_blocks_path_traversal(client):
    c, _ = client
    assert c.get("/uploads/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert c.get("/uploads/nope.png").status_code == 404


def test_api_command_carries_messages(client):
    c, svc = client
    # F-A.9:messages 透传不报错(intent=edit 路由到 EditGraph,这里只验入口契约)
    r = c.post("/command", json={
        "text": "改web颜色为粉色",
        "session_id": "s1",
        "messages": [{"role": "host", "text": "你好", "ts": 1}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "edit"
    assert body["task_id"].startswith("edit-")
    # 收尾:终止后台任务并 join,避免 fixture 关库时后台线程仍在写库。
    svc.decide(body["task_id"], "abort", reason="测试收尾")
    t = svc._threads.get(body["task_id"])
    if t is not None:
        t.join(timeout=8)


# ── F-A.7 通道①:对话式决策回复(/decision 文本解析)──────────
def test_api_decision_parses_text_reply(client):
    c, _ = client
    # verdict 留空 + text 自然语言 → 后端 classify_decision 归一为 verdict。
    r = c.post("/decision", json={"task_id": "t-x", "text": "可以,放行吧"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "pass"
    r2 = c.post("/decision", json={"task_id": "t-x", "text": "不行,打回重做"})
    assert r2.json()["verdict"] == "reject"


def test_api_decision_text_unparseable_400(client):
    c, _ = client
    # 无法判定的对话 → 400,交前端按钮兜底(不擅自替 Host 决策)。
    r = c.post("/decision", json={"task_id": "t-x", "text": "嗯嗯随便"})
    assert r.status_code == 400


def test_api_decision_explicit_verdict_still_works(client):
    c, _ = client
    # 按钮路径:显式 verdict 直接生效,不依赖文本解析。
    r = c.post("/decision", json={"task_id": "t-x", "verdict": "abort"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "abort"
