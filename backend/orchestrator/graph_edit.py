"""EditGraph:Edit 系统编排(M5 / F-E.1 + F-B.2)。

改"系统自身"(agents/orchestrator/skills/templates),不热改 Runtime;一切改动
须经回归 + PR 闸门才生效。镜像 RuntimeGraph 的串行+条件边+回退+人在环结构。

链路(对齐 PRD Edit 全链路 ASCII):
  Host"改系统" → Edit 部长(定位+TODO)→ 工程师(feature 分支 diff)
    → 回归测试官(≥95%):TestSuiteRunner 机械跑通过率先行(劣化直接 reject 不进模型)
         ├ pass  → 变更评审(提 PR)→ [Host 确认 Merge]→ main 生效
         └ reject→ 回退到工程师重做(LoopController,≤3);超限置 need_decision
       异常:git revert 回滚

每节点 = 一次无状态角色调用(NodeRunner,edit 记忆命名空间);git 动作走 GitService
(本地优先 + 受控 PR,默认不自动推远端)。静态 DAG 供 /edit/graph 可视化复用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from backend.config import settings
from backend.core.event_bus import EventBus
from backend.orchestrator.decision_gate import DecisionGate
from backend.orchestrator.edges import Decision, decide_from_verdict
from backend.orchestrator.loop import LoopController
from backend.orchestrator.node_runner import NodeRunner
from backend.schema import Artifact, CompanyState, TaskStatus
from backend.services.edit_workspace import EditWorkspace

EventSink = Callable[[dict[str, Any]], None]

_LOOP_KEY = "edit-quality"

# Edit 图级里程碑事件(落库审计真源 + 推流)。
_PERSIST_GRAPH_EVENTS = frozenset({
    "edit_start", "edit_locate", "edit_change", "edit_commit", "edit_regression",
    "edit_review", "edit_rework", "need_decision", "decision",
    "edit_revert", "edit_done", "restart_required", "done", "error",
})


def _restart_scope(files: list[str]) -> str | None:
    """据被改文件路径判断需重启的范围(F-E.6)。

    backend/** → backend;frontend/** → frontend;两者皆有 → both;
    都不涉及(纯角色 YAML/提示词改动,Runtime 热加载即可)→ None,无需重启。
    """
    touch_backend = any(f.startswith("backend/") for f in files)
    touch_frontend = any(f.startswith("frontend/") for f in files)
    if touch_backend and touch_frontend:
        return "both"
    if touch_backend:
        return "backend"
    if touch_frontend:
        return "frontend"
    return None

# 静态工作流 DAG(F-A.8 可视化:节点=角色/闸门,边=流转语义)。
# role 节点带 role_id,供前端 RoleInspector 下钻角色详情(model_tier/职责/可调 skill+tool)。
_DAG_NODES = [
    {"id": "edit_start", "label": "Host 改系统", "kind": "entry"},
    {"id": "edit-lead-agent", "label": "Edit 部长(定位+TODO)",
     "kind": "role", "role_id": "edit-lead-agent"},
    {"id": "edit-engineer-agent", "label": "工程师(feature 分支 diff)",
     "kind": "role", "role_id": "edit-engineer-agent"},
    {"id": "edit-regression-agent", "label": "回归测试官(≥95%)",
     "kind": "gate", "role_id": "edit-regression-agent"},
    {"id": "edit-review-agent", "label": "变更评审(提 PR)",
     "kind": "role", "role_id": "edit-review-agent"},
    {"id": "host_merge", "label": "Host 确认 Merge", "kind": "decision"},
    {"id": "main", "label": "main 生效", "kind": "terminal"},
    {"id": "revert", "label": "git revert 回滚", "kind": "fallback"},
]
_DAG_EDGES = [
    {"from": "edit_start", "to": "edit-lead-agent", "label": ""},
    {"from": "edit-lead-agent", "to": "edit-engineer-agent", "label": "TODO"},
    {"from": "edit-engineer-agent", "to": "edit-regression-agent", "label": "diff"},
    {"from": "edit-regression-agent", "to": "edit-review-agent", "label": "≥95%"},
    {"from": "edit-regression-agent", "to": "edit-engineer-agent",
     "label": "劣化→回退"},
    {"from": "edit-review-agent", "to": "host_merge", "label": "PR"},
    {"from": "host_merge", "to": "main", "label": "确认 Merge"},
    {"from": "host_merge", "to": "revert", "label": "异常"},
]


@dataclass
class EditResult:
    state: CompanyState
    status: TaskStatus
    final_artifact: Artifact | None
    pr: Any = None
    note: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


class EditGraph:
    def __init__(
        self,
        runner: NodeRunner,
        git_service: Any,
        loop: LoopController | None = None,
        testsuite=None,
        event_sink: EventSink | None = None,
        event_bus: EventBus | None = None,
        decision_gate: DecisionGate | None = None,
        threshold: float | None = None,
        restarter: Any = None,
    ) -> None:
        self._runner = runner
        self._git = git_service
        # 仓库接地层:列真实可改文件 + 读目标真实内容 + search/replace 精确改写。
        # repo_root 与 GitService 同根,保证定位/落盘/diff 三者口径一致。
        self._workspace = EditWorkspace(getattr(git_service, "_dir", None))
        self._loop = loop or LoopController()
        self._testsuite = testsuite
        self._sink = event_sink
        self._bus = event_bus
        self._gate = decision_gate
        self._restarter = restarter
        self._threshold = (
            settings.eval_pass_threshold if threshold is None else threshold
        )
        self._events: list[dict[str, Any]] = []

    # --- 静态 DAG(可视化)---
    @classmethod
    def dag_spec(
        cls, ref: str = "main", changed_targets: list[str] | None = None
    ) -> dict[str, Any]:
        """返回 {nodes, edges, ref};feature ref 时把改动涉及节点标 changed=True。"""
        changed = set(changed_targets or [])
        nodes = []
        for n in _DAG_NODES:
            node = dict(n)
            node["changed"] = bool(ref != "main" and node["id"] in changed)
            nodes.append(node)
        return {"ref": ref, "nodes": nodes, "edges": list(_DAG_EDGES)}

    def _emit(self, event: str, **kw: Any) -> None:
        e = {"event": event, **kw}
        self._events.append(e)
        if self._sink:
            self._sink(e)
        if self._bus is not None and event in _PERSIST_GRAPH_EVENTS:
            task_id = kw.get("task_id") or getattr(self, "_task_id", "")
            payload = {k: v for k, v in kw.items() if k != "task_id"}
            bus_event = "done" if event == "edit_done" else event
            self._bus.emit(task_id, bus_event, role="edit-orchestrator",
                           payload=payload, persist=True)

    def run(self, state: CompanyState, goal: str) -> EditResult:
        self._events = []
        self._task_id = state.task_id
        self._goal = goal
        state.system = "edit"
        self._emit("edit_start", task_id=state.task_id, goal=goal)

        # ── 1. Edit 部长定位 + TODO(F-E.1 定位)──────────────────
        # 接地:把仓库里真实存在的可改文件清单喂给部长,逼其基于事实定位,
        # 不再脑补不存在的文件名(此前"变粉"失败的根因之一)。
        repo_files = self._workspace.list_repo_files()
        files_blob = "\n".join(repo_files) if repo_files else "(空)"
        lead = self._runner.run(
            state, "edit-lead-agent",
            f"宿主诉求(改系统):{goal}\n请定位改动目标(提示词/编排/Skill/Tool),"
            "拆解为 edit TODO Plan,并给出可被 diff/回归核对的 acceptance。\n\n"
            "【仓库真实可改文件清单(targets 必须取自其中,禁止虚构路径)】\n"
            f"{files_blob}",
        )
        targets: list[str] = lead.data.get("targets", []) or []
        todo_plan: list[dict[str, Any]] = lead.data.get("todo_plan", []) or []
        acceptance: list[str] = lead.data.get("acceptance", []) or []
        state.todo_plan = todo_plan
        self._emit("edit_locate", targets=targets, todo_items=len(todo_plan),
                   acceptance=len(acceptance))

        # ── 2~4. 工程师改动 → 回归判定 → 回退(≤3)────────────────
        branch = self._git.branches.feature_name(goal)
        eng: Artifact | None = None
        feedback: Artifact | None = None
        sr_result: Any = None
        while True:
            eng = self._engineer_step(state, goal, targets, todo_plan, feedback)
            # feature 分支落改动(dry-run 默认不触碰真实仓库)
            self._git.checkout_new_branch(branch)
            changes = eng.data.get("changes", []) or []
            # 落盘:闸门开(git.enabled)→ 走 search/replace 精确改写真实文件;
            # dry-run → 只把计划记进 GitService.plan,零风险。绝不再把"改动说明"
            # 当文件正文整段覆盖(此前改坏 styles.css 的根因)。
            if self._git.enabled and changes:
                sr_result = self._workspace.apply_search_replace(changes)
                self._emit("edit_change_apply", applied=sr_result.applied,
                           failed=sr_result.failed, skipped=sr_result.skipped)
                # 以真实改写到的文件为准(供 PR / 重启范围判定),不信模型自报。
                if sr_result.changed_files:
                    eng.artifact.files = list(sr_result.changed_files)
            else:
                planned = {c["path"]: "" for c in changes if c.get("path")}
                if planned:
                    self._git.apply_changes(planned)

            verdict, rate, failed, judge = self._regression(
                state, goal, eng, acceptance
            )
            self._emit("edit_regression", verdict=verdict, pass_rate=rate,
                       threshold=self._threshold, failed_checks=failed,
                       iteration=self._loop.iterations(state, _LOOP_KEY))
            if decide_from_verdict(verdict) == Decision.PASS:
                break

            # 劣化 → 回退到工程师重做
            if not self._loop.can_rework(state, _LOOP_KEY):
                return self._need_decision(
                    state,
                    note=(f"回归劣化回退达上限 {self._loop.iterations(state, _LOOP_KEY)} 次"
                          f"(通过率 {rate:.2%} < {self._threshold:.0%});未过项: {failed}。"),
                    final=eng, branch=branch, acceptance=acceptance,
                )
            n = self._loop.register_reject(state, _LOOP_KEY)
            feedback = judge
            self._emit("edit_rework", loop_key=_LOOP_KEY, iteration=n)

        # ── 4.5 回归通过 → 在 feature 分支提交改动(闸门开才真实 commit)──
        # 没有 commit 就无法 push/merge;dry-run 时 GitService.commit 只记计划。
        if self._git.enabled and sr_result is not None and sr_result.changed_files:
            from backend.services.git_service import PRComposer
            commit_msg = PRComposer.commit_message(
                eng.artifact.summary or goal, todo_ref=todo_plan[0]["id"]
                if todo_plan else "")
            commit = self._git.commit(commit_msg, files=sr_result.changed_files)
            self._emit("edit_commit", branch=branch, sha=commit.output,
                       files=sr_result.changed_files)

        # ── 5. 变更评审 → 提 PR(F-E.1 变更评审)──────────────────
        pr = self._review_step(state, goal, eng, branch)
        self._emit("edit_review", branch=branch, pr_url=pr.pr_url,
                   pushed=pr.pushed, dry_run=pr.dry_run)

        # ── 6. Host 确认 Merge(F-A.7 人在环;不可逆对外动作走 Host)──
        return self._host_merge(state, eng, pr, branch)

    # ── 内部步骤 ───────────────────────────────────────────────
    def _engineer_step(
        self, state: CompanyState, goal: str, targets: list[str],
        todo_plan: list[dict[str, Any]], feedback: Artifact | None,
    ) -> Artifact:
        # 接地:读目标文件的真实内容喂给工程师,让其基于事实产 search/replace,
        # find 必须是文件里真实存在的片段,replace 为新片段(锚点不命中即失败留痕)。
        contents = self._workspace.read_targets(targets)
        blocks = []
        for path, text in contents.items():
            blocks.append(f"=== {path} ===\n{text}")
        files_blob = "\n\n".join(blocks) if blocks else "(未读到目标文件内容)"
        task = (f"改系统目标:{goal}\nEdit TODO:{todo_plan}\n"
                "请基于下方目标文件的【真实内容】产出精确改动 changes,每项为 "
                "{path, find, replace, summary}:find 必须是文件中真实存在、"
                "可唯一定位的原始片段,replace 为替换后的新片段;不直接 push/Merge。\n\n"
                f"【目标文件真实内容】\n{files_blob}")
        if feedback is not None:
            task += ("\n\n【上一轮回归未过,请针对性修复】\n"
                     f"未过项:{feedback.data.get('failed_checks', feedback.issues)}\n"
                     f"建议:{feedback.data.get('suggestion', feedback.handoff_notes)}")
        art = self._runner.run(state, "edit-engineer-agent", task, upstream=feedback)
        self._emit("edit_change", status=art.status.value,
                   files=art.artifact.files)
        return art

    def _regression(
        self, state: CompanyState, goal: str,
        eng: Artifact, acceptance: list[str],
    ) -> tuple[str, float, list[str], Artifact | None]:
        """F-E.3 两段判定:回归套件机械跑通过率先行 + 模型语义兜底。"""
        rate = 1.0
        failed: list[str] = []
        if self._testsuite is not None:
            report = self._testsuite.run()
            rate = report.pass_rate
            failed = [r.case_id for r in report.results if not r.passed]
            if report.below_threshold:
                # 劣化 → 直接 reject,不进模型(省 token + 硬约束优先)
                judge = eng.model_copy(deep=True)
                judge.data = {
                    "verdict": "reject",
                    "pass_rate": rate,
                    "failed_checks": [str(f) for f in failed],
                    "suggestion": f"回归通过率 {rate:.2%} < {self._threshold:.0%},请修复劣化用例。",
                }
                return "reject", rate, [str(f) for f in failed], judge

        # 回归达标 → 模型语义兜底确认
        judge_task = (
            f"改系统目标:{goal}\n验收标准:{acceptance}\n"
            f"回归套件通过率:{rate:.2%}(阈值 {self._threshold:.0%},已达标)。\n"
            "请做语义层回归确认:本次改动是否安全、有无明显劣化。"
            "输出 verdict=pass|reject 及理由与建议。"
        )
        judge = self._runner.run(
            state, "edit-regression-agent", judge_task, upstream=eng
        )
        verdict = judge.data.get("verdict", "pass")
        failed = judge.data.get("failed_checks", []) or []
        return verdict, rate, failed, judge

    def _review_step(self, state: CompanyState, goal: str,
                     eng: Artifact, branch: str) -> Any:
        review = self._runner.run(
            state, "edit-review-agent",
            f"改系统目标:{goal}\n回归已 ≥95% 通过。请组织 PR(标题/正文/关联 badcase),"
            "提交 PR 等 Host 确认 Merge;不替 Host 做 Merge。",
            upstream=eng,
        )
        summary = review.data.get("pr_summary") or review.artifact.summary or goal
        badcase_ref = review.data.get("badcase_ref", "")
        return self._git.open_pr(branch, summary, badcase_ref)

    def _host_merge(self, state: CompanyState, eng: Artifact,
                    pr: Any, branch: str) -> EditResult:
        state.status = TaskStatus.NEED_DECISION
        self._runner.checkpoint(state)
        self._emit("need_decision",
                   note=f"PR 已就绪({pr.pr_url}),等 Host 确认 Merge 到 main。",
                   pr_url=pr.pr_url)

        if self._gate is None:
            # 离线/测试:无 gate,产出 PR 等 Host(不自行 Merge,符合受控 PR 约束)。
            return EditResult(state, TaskStatus.NEED_DECISION, eng, pr=pr,
                              note="PR 就绪,等 Host 确认 Merge", events=self._events)

        decision = self._gate.wait(state.task_id)
        if decision is None:
            self._emit("need_decision", note="等待 Host 确认 Merge 超时,PR 保留。")
            return EditResult(state, TaskStatus.NEED_DECISION, eng, pr=pr,
                              note="等待 Merge 超时", events=self._events)

        self._emit("decision", verdict=decision.verdict, reason=decision.reason)
        if decision.verdict == "pass":
            # Host 确认 Merge:main 生效(真实 Merge 由 Host 在 GitHub 完成)。
            state.status = TaskStatus.DONE
            self._runner.checkpoint(state)
            self._emit("edit_done", task_id=state.task_id, branch=branch,
                       pr_url=pr.pr_url)
            # F-E.6:改动落到 backend/** 或 frontend/** 才需重启才生效。
            # 默认只发 restart_required 信号(闸门关 → dry-run),由 Host 手动重启;
            # 闸门开 → restart_service 脱离当前进程重启,health 失败回滚。
            self._maybe_restart(eng)
            return EditResult(state, TaskStatus.DONE, eng, pr=pr,
                              note="Host 确认 Merge,main 生效", events=self._events)

        if decision.verdict == "abort":
            # 异常:git revert 回滚 + 终止。
            self._git.revert("HEAD")
            self._emit("edit_revert", branch=branch, reason=decision.reason)
            state.status = TaskStatus.NEED_DECISION
            self._runner.checkpoint(state)
            return EditResult(state, TaskStatus.NEED_DECISION, eng, pr=pr,
                              note=f"Host 否决,已 git revert: {decision.reason}",
                              events=self._events)

        # reject:不 Merge,保留 PR 等后续。
        state.status = TaskStatus.NEED_DECISION
        self._runner.checkpoint(state)
        return EditResult(state, TaskStatus.NEED_DECISION, eng, pr=pr,
                          note=f"Host 暂不 Merge: {decision.reason}",
                          events=self._events)

    def _maybe_restart(self, eng: Artifact) -> None:
        """F-E.6:Merge 后据改动范围决定是否需重启;发 restart_required 信号。

        改动若落在 backend/** 或 frontend/** 才需重启;纯角色 YAML/提示词改动
        Runtime 下次调用即生效,无需重启。受 edit_auto_restart_enabled 闸门控制:
        闸门关 → restarter 回 dry-run(restart_required),由 Host 手动重启;
        闸门开 → 脱离当前进程真正重启,health 失败回滚。
        """
        files = list(eng.artifact.files or [])
        scope = _restart_scope(files)
        if scope is None:
            return
        result: dict[str, Any] = {}
        if self._restarter is not None:
            try:
                result = self._restarter.restart(scope).as_dict()
            except Exception as exc:  # noqa: BLE001 — 重启失败不拖垮收口
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._emit("restart_required", scope=scope, files=sorted(files),
                   restart=result)

    def _need_decision(self, state: CompanyState, note: str,
                       final: Artifact | None, branch: str,
                       acceptance: list[str]) -> EditResult:
        state.status = TaskStatus.NEED_DECISION
        self._runner.checkpoint(state)
        self._emit("need_decision", note=note)
        if self._gate is None:
            return EditResult(state, TaskStatus.NEED_DECISION, final,
                              note=note, events=self._events)
        decision = self._gate.wait(state.task_id)
        if decision is None:
            return EditResult(state, TaskStatus.NEED_DECISION, final,
                              note=f"{note}(超时)", events=self._events)
        self._emit("decision", verdict=decision.verdict, reason=decision.reason)
        if decision.verdict == "pass":
            # Host 放行:重置回退计数,直接走评审提 PR。
            state.loop_counters[_LOOP_KEY] = 0
            pr = self._review_step(state, self._goal, final, branch)
            self._emit("edit_review", branch=branch, pr_url=pr.pr_url,
                       pushed=pr.pushed, dry_run=pr.dry_run)
            return self._host_merge(state, final, pr, branch)
        return EditResult(state, TaskStatus.NEED_DECISION, final,
                          note=f"Host 终止 Edit: {decision.reason}",
                          events=self._events)
