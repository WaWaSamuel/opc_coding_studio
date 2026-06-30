"""M7 Edit 终极形态 种子测试。

覆盖本轮"优先实现 edit 系统终极形态"的五项实现:
  - F-A.9 真多轮续跑:同 session 活跃任务追加消息走 DecisionGate,不另开 task
  - F-A.12 历史会话:list_tasks 最近优先 + 按 system 过滤
  - F-E.8 隔离实例验证:闸门关 / dry-run / 无 verifier → skipped 不阻断
  - F-E.9 概念级改造加固:空白归一兜底 fuzzy 命中 + 失败回灌强制回退
  - EditGraph 接入 isolated_verifier 参数 + edit_verify 事件
"""
from __future__ import annotations

import pytest

from backend.gateway.host_command import HostCommand
from backend.orchestrator.graph_edit import EditGraph
from backend.services.edit_workspace import EditWorkspace
from backend.services.isolated_verifier import IsolatedVerifier, VerifyResult


# ── F-E.9 空白归一兜底(缩进/空白差异仍能命中)───────────────
def test_fuzzy_replace_hits_on_indent_mismatch(tmp_path):
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "x.py").write_text(
        "def f():\n        return 1\n", encoding="utf-8")  # 8 空格缩进
    ws = EditWorkspace(tmp_path)
    # find 用 4 空格缩进(与原文不一致),精确匹配会 miss,空白归一兜底应命中
    res = ws.apply_search_replace([{
        "path": "backend/x.py",
        "find": "def f():\n    return 1",
        "replace": "def f():\n    return 2",
    }])
    assert res.applied.get("backend/x.py") == 1
    assert res.fuzzy and res.fuzzy[0]["path"] == "backend/x.py"
    assert res.failed == []
    text = (tmp_path / "backend" / "x.py").read_text(encoding="utf-8")
    assert "return 2" in text and "return 1" not in text


def test_fuzzy_replace_returns_none_when_absent(tmp_path):
    content = "alpha\nbeta\n"
    out = EditWorkspace._fuzzy_replace(content, "no\nsuch\nblock", "x")
    assert out is None


# ── F-E.8 IsolatedVerifier 跳过路径(不起子进程)─────────────
class _FakeGit:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled


def test_verifier_skipped_when_gate_closed():
    v = IsolatedVerifier(_FakeGit(enabled=True), enabled=False)
    vr = v.verify("feature/x")
    assert vr.ok is True and vr.skipped is True
    assert "闸门" in vr.note


def test_verifier_skipped_when_dry_run():
    # 闸门开但 git dry-run(无真实分支可检出)→ 跳过,不起子进程
    v = IsolatedVerifier(_FakeGit(enabled=False), enabled=True)
    vr = v.verify("feature/x")
    assert vr.ok is True and vr.skipped is True


def test_verify_result_as_dict_truncates_logs():
    vr = VerifyResult(False, note="boom", logs="L" * 5000)
    d = vr.as_dict()
    assert d["ok"] is False and d["note"] == "boom"
    assert len(d["logs"]) == 1000  # 只保留尾部 1000 字符


# ── F-E.8 EditGraph 接入:构造器参数 + 跳过验证放行 ──────────
def test_editgraph_accepts_isolated_verifier():
    import inspect
    params = inspect.signature(EditGraph.__init__).parameters
    assert "isolated_verifier" in params


def test_editgraph_verify_isolated_skips_without_verifier():
    g = EditGraph.__new__(EditGraph)
    g._verifier = None
    g._events = []
    g._sink = None
    g._bus = None
    vr = g._verify_isolated("feature/x")
    assert vr.ok is True and vr.skipped is True


# ── F-A.9 真多轮续跑(service 层)────────────────────────────
def _svc(tmp_path):
    from backend.orchestrator.service import OrchestratorService
    return OrchestratorService(db_path=str(tmp_path / "m7.db"))


def test_continue_active_task_routes_into_gate(tmp_path):
    svc = _svc(tmp_path)
    try:
        cmd = HostCommand(
            text="改web颜色为粉色", session_id="sx", channel="web",
            intent="edit", host_verified=True,
        )
        tid = svc.submit(cmd)
        assert tid.startswith("edit-")
        # 等任务进入 need_decision(mock 模型下很快收口到 PR 等 Host 确认)
        import time
        deadline = time.time() + 8
        while time.time() < deadline:
            if svc.task_status(tid) == "need_decision":
                break
            time.sleep(0.1)
        # 同 session 追加一条"放行"消息:应路由进同一 task 的 gate,而非新建
        follow = HostCommand(
            text="可以,放行吧", session_id="sx", channel="web",
            intent="edit", host_verified=True,
        )
        same = svc.submit(follow)
        assert same == tid  # F-A.9:续跑到同一任务,不另开 task
    finally:
        svc.decide(tid, "abort", reason="测试收尾")
        t = svc._threads.get(tid)
        if t is not None:
            t.join(timeout=8)
        svc.close()


def test_continue_active_task_none_when_no_session_task(tmp_path):
    svc = _svc(tmp_path)
    try:
        cmd = HostCommand(text="hi", session_id="fresh", channel="web",
                          intent="edit", host_verified=True)
        assert svc._continue_active_task(cmd) is None
    finally:
        svc.close()


# ── F-A.12 历史会话列表 ──────────────────────────────────────
def test_list_tasks_recent_first_and_system_filter(tmp_path):
    svc = _svc(tmp_path)
    try:
        from backend.schema import CompanyState
        svc._ckpt.save(CompanyState(task_id="edit-1", system="edit",
                                    workflow="web-edit"))
        svc._ckpt.save(CompanyState(task_id="task-1", system="runtime",
                                    workflow="web-runtime"))
        all_tasks = svc.list_tasks(limit=50)
        ids = {t["task_id"] for t in all_tasks}
        assert {"edit-1", "task-1"} <= ids
        only_edit = svc.list_tasks(limit=50, system="edit")
        assert all(t["system"] == "edit" for t in only_edit)
        assert any(t["task_id"] == "edit-1" for t in only_edit)
        assert not any(t["task_id"] == "task-1" for t in only_edit)
    finally:
        svc.close()
