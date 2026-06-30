"""M5 自迭代 + 版本管理 种子测试。

覆盖:
  - GitService:dry-run(默认保命)+ 真实 tmp git repo(分支/改动/commit/diff/revert)
  - EditGraph:Edit 全链路端到端(无 gate → PR 就绪等 Host;有 gate → Host 确认 Merge)
  - BadcaseCollector:异常信号 → 结构化入测试集
  - TestSuiteRunner + EvalReporter:通过率报告 + 阈值判定
  - 种子用例:load_seed_testcases 去重载入
  - Scheduler:run_job_now / 异常收口 / job_status
  - OrchestratorService:edit intent → EditGraph + Edit 辅助方法
  - FastAPI:/edit/graph、/edit/pr、/edit/testsuite/{run,seed}
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import pytest

from backend.core.event_bus import EventBus
from backend.core.model_adapter.base import ModelAdapter
from backend.gateway.host_command import HostCommand
from backend.orchestrator.decision_gate import Decision, DecisionGate
from backend.orchestrator.graph_edit import EditGraph
from backend.orchestrator.loop import LoopController
from backend.orchestrator.service import OrchestratorService
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState, InvokeResult, TaskStatus
from backend.services.badcase import BadcaseCollector
from backend.services.git_service import GitService, PRComposer
from backend.services.scheduler import Scheduler
from backend.services.seed_testcases import SEED_CASES, load_seed_testcases
from backend.services.testsuite import (
    CaseResult,
    EvalReporter,
    TestSuiteRunner,
)

EDIT_GOAL = "改工作流:优化 loop-judge 角色提示词,减少误判返工。"
EDIT_ACCEPTANCE = ["改动经回归 ≥95%", "提 PR 等 Host 确认 Merge"]


# ── 角色感知脚本化 Adapter(覆盖 runtime + edit 角色,离线确定性)──
def _which_role(messages: list[dict[str, str]]) -> str:
    system = messages[0]["content"]
    for rid in (
        "ceo-orchestrator-agent", "pm-prd-agent", "dev-lead-agent",
        "loop-judge-agent", "qa-acceptance-agent",
        "backend-engineer-agent", "frontend-engineer-agent",
        "edit-lead-agent", "edit-engineer-agent",
        "edit-regression-agent", "edit-review-agent",
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


class EditScriptedAdapter(ModelAdapter):
    """Edit happy-path:部长定位 → 工程师 diff → 回归 pass → 评审提 PR。"""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def invoke(self, messages, schema=None, tier="large") -> InvokeResult:
        role = _which_role(messages)
        self.calls[role] = self.calls.get(role, 0) + 1
        return InvokeResult(content=self._content_for(role),
                            tokens_in=40, tokens_out=60, latency_ms=1)

    def _content_for(self, role: str) -> str:
        if role == "edit-lead-agent":
            return _j(role, data={
                "targets": ["backend/core/roles/specs/loop-judge-agent.yaml"],
                "todo_plan": [{"id": "E1", "desc": "收紧 loop-judge 判定口径",
                               "owner_role": "edit-engineer-agent", "status": "todo"}],
                "acceptance": EDIT_ACCEPTANCE,
            })
        if role == "edit-engineer-agent":
            return _j(role, files=["backend/core/roles/specs/loop-judge-agent.yaml"],
                      summary="收紧 loop-judge 提示词", data={
                          "branch_hint": "loop-judge-tighten",
                          "changes": [{
                              "path": "backend/core/roles/specs/loop-judge-agent.yaml",
                              "summary": "只在确有语义不满足时 reject",
                              "find": "只在确有语义层面的不满足时才 reject",
                              "replace": "仅在确有语义层面的不满足时才 reject(收紧口径)",
                          }],
                      })
        if role == "edit-regression-agent":
            return _j(role, data={"verdict": "pass", "pass_rate": 1.0,
                                  "failed_checks": [], "reason": "无劣化"})
        if role == "edit-review-agent":
            return _j(role, data={"pr_title": "[Edit] 优化 loop-judge",
                                  "pr_summary": "收紧 loop-judge 判定口径",
                                  "badcase_ref": "badcase:demo"})
        return _j(role)


# ── GitService:dry-run(保命默认)──────────────────────────────
def test_git_service_dry_run_default(tmp_path):
    git = GitService(repo_dir=str(tmp_path), enabled=False)
    assert git.enabled is False
    assert git.can_push is False
    r = git.checkout_new_branch("feature/x")
    assert r.ok and "[dry-run]" in r.note
    # 写路径受 F-E.7 白名单约束:用纳管目录内的路径(backend/**)。
    git.apply_changes({"backend/a.py": "print(1)", "backend/b.py": "print(2)"})
    # dry-run diff 从 plan 推 planned change
    diff = git.diff("master", None)
    assert "planned change" in diff and "backend/a.py" in diff
    pr = git.open_pr("feature/x", "改点东西", badcase_ref="badcase:1")
    assert pr.dry_run is True and pr.pushed is False
    assert "compare" in pr.pr_url
    # 计划留痕含分支/写盘/PR
    actions = [a["action"] for a in git.plan]
    assert "create_branch" in actions and "apply_changes" in actions


def test_pr_composer_records_badcase():
    msg = PRComposer.commit_message("修角色提示词", badcase_ref="badcase:7",
                                    todo_ref="E1")
    assert "Badcase: badcase:7" in msg and "Optimization: E1" in msg
    draft = PRComposer.compose("feature/y", "概述", "+ new line",
                               badcase_ref="badcase:7")
    assert "概述" in draft.body and "badcase:7" in draft.body


# ── GitService:真实 tmp git repo ──────────────────────────────
def _init_git_repo(path) -> None:
    def g(*args):
        subprocess.run(["git", *args], cwd=str(path), check=True,
                       capture_output=True, text=True)
    g("init", "-b", "master")
    g("config", "user.email", "t@t.io")
    g("config", "user.name", "t")
    (path / "seed.txt").write_text("base\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-m", "init")


def test_git_service_enabled_real_repo(tmp_path):
    _init_git_repo(tmp_path)
    git = GitService(repo_dir=str(tmp_path), enabled=True, push_enabled=False,
                     main_branch="master")
    assert git.current_branch() == "master"
    git.checkout_new_branch("feature/real")
    assert git.current_branch() == "feature/real"
    git.apply_changes({"backend/new.py": "print('hi')\n"})
    commit = git.commit("edit: add new.py")
    assert commit.ok and commit.output  # sha
    diff = git.diff("master", "feature/real")
    assert "backend/new.py" in diff
    rv = git.revert("HEAD")
    assert rv.ok


# ── EditGraph:端到端(无 gate → PR 就绪等 Host)────────────────
def _edit_service(tmp_path) -> OrchestratorService:
    return OrchestratorService(db_path=str(tmp_path / "edit.db"),
                               adapter=EditScriptedAdapter())


def test_edit_graph_pr_ready_without_gate(tmp_path):
    svc = _edit_service(tmp_path)
    try:
        runner = svc._new_runner(namespace="edit")
        graph = EditGraph(runner, GitService(enabled=False),
                          loop=LoopController(), testsuite=None,
                          decision_gate=None)
        state = CompanyState(task_id="edit-1", system="edit", workflow="web-edit")
        result = graph.run(state, EDIT_GOAL)
        assert result.status == TaskStatus.NEED_DECISION
        assert result.pr is not None and result.pr.dry_run is True
        assert "compare" in result.pr.pr_url
        evs = [e["event"] for e in result.events]
        assert "edit_locate" in evs and "edit_change" in evs
        assert "edit_regression" in evs and "edit_review" in evs
    finally:
        svc.close()


def test_edit_graph_host_merge_done(tmp_path):
    svc = _edit_service(tmp_path)
    try:
        gate = DecisionGate()
        # 预置 Host 放行决策(gate.submit 先存,wait 立即返回)
        gate.submit("edit-2", Decision(verdict="pass", reason="放行"))
        runner = svc._new_runner(namespace="edit")
        graph = EditGraph(runner, GitService(enabled=False),
                          loop=LoopController(), testsuite=None,
                          decision_gate=gate)
        state = CompanyState(task_id="edit-2", system="edit", workflow="web-edit")
        result = graph.run(state, EDIT_GOAL)
        assert result.status == TaskStatus.DONE
        evs = [e["event"] for e in result.events]
        assert "edit_done" in evs and "decision" in evs
    finally:
        svc.close()


def test_edit_graph_enabled_real_search_replace_and_commit(tmp_path):
    """闸门开:Edit 在真实 git 仓库里 search/replace 改文件并 commit(真改代码)。"""
    # 在 tmp repo 里放一个含工程师 find 锚点的目标文件
    target_rel = "backend/core/roles/specs/loop-judge-agent.yaml"
    target = tmp_path / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "role_prompt: |\n  只在确有语义层面的不满足时才 reject\n", encoding="utf-8")

    def g(*args):
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True,
                       capture_output=True, text=True)
    g("init", "-b", "master")
    g("config", "user.email", "t@t.io")
    g("config", "user.name", "t")
    g("add", "-A")
    g("commit", "-m", "init")

    svc = OrchestratorService(db_path=str(tmp_path / "edit.db"),
                              adapter=EditScriptedAdapter())
    try:
        gate = DecisionGate()
        gate.submit("edit-3", Decision(verdict="pass", reason="放行"))
        runner = svc._new_runner(namespace="edit")
        git = GitService(repo_dir=str(tmp_path), enabled=True,
                         push_enabled=False, main_branch="master")
        graph = EditGraph(runner, git, loop=LoopController(),
                          testsuite=None, decision_gate=gate)
        state = CompanyState(task_id="edit-3", system="edit", workflow="web-edit")
        result = graph.run(state, EDIT_GOAL)
        assert result.status == TaskStatus.DONE
        evs = [e["event"] for e in result.events]
        assert "edit_change_apply" in evs and "edit_commit" in evs
        # 文件被真实精确改写(锚点替换),不是整文件覆盖
        text = target.read_text(encoding="utf-8")
        assert "仅在确有语义层面的不满足时才 reject(收紧口径)" in text
        assert "role_prompt: |" in text  # 其余内容保留
        # feature 分支上确有一次提交
        log = subprocess.run(["git", "log", "--oneline"], cwd=str(tmp_path),
                             capture_output=True, text=True).stdout
        assert log.count("\n") >= 2  # init + edit commit
    finally:
        svc.close()


# ── BadcaseCollector ─────────────────────────────────────────
def test_badcase_collector_inspect_and_collect(tmp_path):
    repo = SqliteRepo(str(tmp_path / "bc.db"))
    try:
        tid = "task-bad"
        repo.append_log({"ts": "t", "task_id": tid, "system": "runtime",
                         "role": "orchestrator", "event": "graph_start",
                         "tokens": 0, "latency_ms": 0,
                         "note": json.dumps({"goal": "做个会失败的活"})})
        repo.append_log({"ts": "t", "task_id": tid, "system": "runtime",
                         "role": "orchestrator", "event": "error",
                         "tokens": 0, "latency_ms": 0, "note": "boom"})
        state = CompanyState(task_id=tid, system="runtime", workflow="web-runtime")
        state.payload = {"intent": "runtime"}
        repo.save_checkpoint(state)

        collector = BadcaseCollector(repo)
        sig = collector.inspect_task(tid)
        assert sig.hit and "error" in sig.reasons and sig.goal
        rid = collector.collect(tid)
        assert rid is not None
        # 去重:二次 collect 不再入库
        assert collector.collect(tid) is None
        cases = repo.list_testcases()
        assert any(c["dedup_key"] == f"badcase:{tid}" for c in cases)
    finally:
        repo.close()


def test_badcase_no_signal_no_insert(tmp_path):
    repo = SqliteRepo(str(tmp_path / "bc2.db"))
    try:
        tid = "task-ok"
        repo.append_log({"ts": "t", "task_id": tid, "system": "runtime",
                         "role": "o", "event": "graph_start", "tokens": 0,
                         "latency_ms": 0, "note": json.dumps({"goal": "顺利"})})
        repo.append_log({"ts": "t", "task_id": tid, "system": "runtime",
                         "role": "o", "event": "done", "tokens": 0,
                         "latency_ms": 0, "note": "{}"})
        assert BadcaseCollector(repo).collect(tid) is None
    finally:
        repo.close()


# ── TestSuiteRunner + EvalReporter ───────────────────────────
def test_eval_reporter_threshold():
    rep = EvalReporter(threshold=0.95)
    results = [CaseResult(i, f"g{i}", passed=(i != 0)) for i in range(20)]
    report = rep.report(results)  # 19/20 = 0.95,不低于阈值
    assert report.pass_rate == pytest.approx(0.95)
    assert report.below_threshold is False
    bad = rep.report([CaseResult(0, "g", False), CaseResult(1, "g", True)])
    assert bad.below_threshold is True


def test_testsuite_runner_with_stub(tmp_path):
    repo = SqliteRepo(str(tmp_path / "ts.db"))
    try:
        for c in SEED_CASES:
            repo.save_testcase(c)

        def stub_runner(case: dict) -> CaseResult:
            return CaseResult(case["id"], case["goal"], passed=True, note="ok")

        runner = TestSuiteRunner(repo, stub_runner, threshold=0.95)
        report = runner.run()
        assert report.total == len(SEED_CASES)
        assert report.passed == len(SEED_CASES)
        assert report.below_threshold is False
        d = report.as_dict()
        assert d["pass_rate"] == 1.0
    finally:
        repo.close()


def test_load_seed_testcases_dedup(tmp_path):
    repo = SqliteRepo(str(tmp_path / "seed.db"))
    try:
        added = load_seed_testcases(repo)
        assert added == len(SEED_CASES)
        # 二次载入不重复
        assert load_seed_testcases(repo) == 0
    finally:
        repo.close()


# ── Scheduler ────────────────────────────────────────────────
def test_scheduler_run_job_now_and_status():
    sched = Scheduler()
    box = {"n": 0}

    def job():
        box["n"] += 1
        return box["n"]

    sched.add_job("counter", interval_seconds=3600, func=job)
    assert sched.run_job_now("counter") == 1
    st = sched.job_status("counter")
    assert st["runs"] == 1 and st["last_error"] == ""


def test_scheduler_captures_job_error():
    sched = Scheduler()

    def boom():
        raise RuntimeError("fail")

    sched.add_job("boom", interval_seconds=3600, func=boom)
    assert sched.run_job_now("boom") is None
    st = sched.job_status("boom")
    assert "RuntimeError" in st["last_error"]


# ── OrchestratorService:edit intent 分流 + 辅助方法 ──────────
def _drain(svc: OrchestratorService, task_id: str, gate_pass: bool = True,
           timeout: float = 12.0) -> list[dict[str, Any]]:
    """订阅事件流;遇 need_decision 自动回灌 pass,直到收口。"""
    q = svc.subscribe(task_id)
    events: list[dict[str, Any]] = []
    deadline = time.time() + timeout
    decided = False
    try:
        while time.time() < deadline:
            try:
                item = q.get(True, 1.0)
            except Exception:
                continue
            if EventBus.is_sentinel(item):
                break
            events.append(item)
            if (item.get("event") == "need_decision" and not decided
                    and gate_pass):
                decided = True
                svc.decide(task_id, "pass", reason="放行")
    finally:
        svc.unsubscribe(task_id, q)
    return events


def test_service_edit_intent_runs_edit_graph(tmp_path):
    svc = _edit_service(tmp_path)
    try:
        cmd = HostCommand(channel="web", session_id="s-edit", text=EDIT_GOAL,
                          host_verified=True, intent="edit")
        task_id = svc.submit(cmd)
        assert task_id.startswith("edit-")
        events = _drain(svc, task_id)
        svc._threads[task_id].join(timeout=8)
        types = [e["event"] for e in events]
        # Edit 链路里程碑应出现
        assert "edit_locate" in types or "edit_change" in types
        # Host 放行后收口
        assert svc.task_status(task_id) == TaskStatus.DONE.value
    finally:
        svc.close()


def test_service_edit_helpers(tmp_path):
    svc = _edit_service(tmp_path)
    try:
        spec = svc.edit_graph(ref="main")
        assert spec["ref"] == "main"
        assert any(n["id"] == "edit-engineer-agent" for n in spec["nodes"])
        assert spec["git"]["main_branch"]
        # 种子载入 + 回归报告
        assert svc.load_seed_testcases() == len(SEED_CASES)
        report = svc.run_testsuite()
        assert report["total"] == len(SEED_CASES)
        # 受控提 PR(dry-run)
        pr = svc.submit_edit_pr("feature/z", "概述")
        assert pr["dry_run"] is True and "compare" in pr["pr_url"]
    finally:
        svc.close()


# ── FastAPI:/edit/* ─────────────────────────────────────────
@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from backend.api.app import create_app
    svc = OrchestratorService(db_path=str(tmp_path / "api.db"),
                              adapter=EditScriptedAdapter())
    app = create_app(svc)
    with TestClient(app) as c:
        yield c, svc
    svc.close()


def test_api_edit_graph(client):
    c, _ = client
    r = c.get("/edit/graph", params={"ref": "main"})
    assert r.status_code == 200
    body = r.json()
    assert body["ref"] == "main"
    assert len(body["nodes"]) >= 6 and len(body["edges"]) >= 6


def test_api_edit_pr_dry_run(client):
    c, _ = client
    r = c.post("/edit/pr", json={"branch": "feature/api", "summary": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["pushed"] is False
    assert "compare" in body["pr_url"]


def test_api_edit_testsuite_seed_and_run(client):
    c, _ = client
    seeded = c.post("/edit/testsuite/seed").json()
    assert seeded["added"] == len(SEED_CASES)
    report = c.post("/edit/testsuite/run").json()
    assert report["total"] == len(SEED_CASES)
    assert "pass_rate" in report
