"""RuntimeGraph:Runtime 串行编排(M2 业务流 PoC)。

PRD 第三部分 M2:用最朴素串行编排端到端跑通一条真实业务流。
覆盖 F-B.1(状态图)/B.3(CEO 路由)/B.4(部长拆解 TODO)/B.6(执行角色)
/B.7(Loop 规则先行+模型兜底)/F-D.1(业务回退≤3)/F-A.4(流转事件)。

链路(串行版,并行留 M3):
  CEO 路由 → 开发部长拆解(TODO+acceptance)→ 后端执行
    → Loop 判定(规则先行 run_rule_checks,硬约束不过直接 reject 不进模型;
       全过再由 loop-judge-agent 语义兜底)
       ├ pass  → 需求验收(qa-acceptance-agent 语义)
       └ reject→ 业务回退到执行重做(LoopController,≤3);超限置 need_decision 等 Host
    → 部长汇总 → done

每节点 = 一次无状态角色调用(NodeRunner);角色间只传结构化 Artifact。
LangGraph StateGraph 在 M4 接入时复用同一套节点/判定语义;M2 先用纯 Python 串行驱动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from backend.orchestrator.edges import Decision, decide_from_verdict
from backend.orchestrator.loop import LoopController
from backend.orchestrator.node_runner import NodeRunner
from backend.orchestrator.rule_checks import run_rule_checks
from backend.schema import Artifact, CompanyState, TaskStatus

EventSink = Callable[[dict[str, Any]], None]

_LOOP_KEY = "build-quality"


@dataclass
class RuntimeResult:
    state: CompanyState
    status: TaskStatus
    final_artifact: Artifact | None
    note: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


class RuntimeGraph:
    def __init__(
        self,
        runner: NodeRunner,
        loop: LoopController | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._runner = runner
        self._loop = loop or LoopController()
        self._sink = event_sink
        self._events: list[dict[str, Any]] = []

    def _emit(self, event: str, **kw: Any) -> None:
        e = {"event": event, **kw}
        self._events.append(e)
        if self._sink:
            self._sink(e)

    def run(self, state: CompanyState, goal: str) -> RuntimeResult:
        self._events = []
        self._emit("graph_start", task_id=state.task_id, goal=goal)

        # ── 1. CEO 路由分流(F-B.3)─────────────────────────────
        ceo = self._runner.run(state, "ceo-orchestrator-agent", goal)
        department = ceo.data.get("department", "engineering")
        is_major = bool(ceo.data.get("is_major", False))
        self._emit("ceo_route", department=department, is_major=is_major)

        # ── 2. 开发部长拆解 + TODO Plan(F-B.4)──────────────────
        lead_task = (
            f"业务目标:{goal}\n"
            f"CEO 路由:目标部门={department}, 是否重大={is_major}。\n"
            "请拆解为可执行的 TODO Plan,并给出每条验收标准(acceptance)。"
        )
        plan = self._runner.run(state, "dev-lead-agent", lead_task, upstream=ceo)
        todo_plan: list[dict[str, Any]] = plan.data.get("todo_plan", [])
        acceptance: list[str] = plan.data.get("acceptance", [])
        state.todo_plan = todo_plan
        self._emit("dev_plan", todo_items=len(todo_plan), acceptance=len(acceptance))

        # ── 3~5. 执行 → Loop 判定 → 业务回退(≤3)───────────────
        build: Artifact | None = None
        judge_feedback: Artifact | None = None
        while True:
            build = self._build_step(state, goal, todo_plan, judge_feedback)

            verdict, failed, judge = self._loop_judge(state, goal, build, acceptance)
            self._emit(
                "loop_judge",
                verdict=verdict,
                failed_checks=failed,
                iteration=self._loop.iterations(state, _LOOP_KEY),
            )
            if decide_from_verdict(verdict) == Decision.PASS:
                break

            # reject:业务回退
            if not self._loop.can_rework(state, _LOOP_KEY):
                return self._need_decision(
                    state,
                    note=(
                        f"Loop 回退达上限 {self._loop.iterations(state, _LOOP_KEY)} 次仍未通过质量判定;"
                        f"未过项: {failed}。置 need_decision 等 Host 拍板。"
                    ),
                    final=build,
                )
            n = self._loop.register_reject(state, _LOOP_KEY)
            judge_feedback = judge
            self._emit("rework", loop_key=_LOOP_KEY, iteration=n)

        # ── 6. 需求验收(F-B.6 需求验收角色,语义)──────────────
        accept_task = (
            f"业务目标:{goal}\n"
            f"验收标准(逐条核对):{acceptance}\n"
            "请对上一棒交付物做需求验收,逐条判断是否达标。"
        )
        accept = self._runner.run(
            state, "qa-acceptance-agent", accept_task, upstream=build
        )
        accept_verdict = accept.data.get("verdict", "pass")
        self._emit("acceptance", verdict=accept_verdict)
        if decide_from_verdict(accept_verdict) != Decision.PASS:
            if not self._loop.can_rework(state, _LOOP_KEY):
                return self._need_decision(
                    state,
                    note=f"需求验收未通过且回退已达上限: {accept.issues}",
                    final=accept,
                )
            # 验收不过也回退到执行(同一 build 循环)
            self._loop.register_reject(state, _LOOP_KEY)
            judge_feedback = accept
            self._emit("rework", loop_key=_LOOP_KEY, reason="acceptance_reject")
            # 重新执行一轮(尾递归式,简单起见再走一遍构建+判定)
            return self._rerun_tail(state, goal, todo_plan, acceptance, judge_feedback)

        # ── 7. 部长汇总 → CEO(F-B.4 验收汇总)────────────────────
        summary = self._runner.run(
            state,
            "dev-lead-agent",
            f"业务目标:{goal}\n请基于已通过验收的交付物做最终汇总,回报 CEO。",
            upstream=accept,
        )
        state.status = TaskStatus.DONE
        self._runner.checkpoint(state)
        self._emit("graph_done", task_id=state.task_id)
        return RuntimeResult(
            state=state,
            status=TaskStatus.DONE,
            final_artifact=summary,
            note="电商业务流 PoC 端到端跑通",
            events=self._events,
        )

    # ── 内部步骤 ───────────────────────────────────────────────
    def _build_step(
        self,
        state: CompanyState,
        goal: str,
        todo_plan: list[dict[str, Any]],
        feedback: Artifact | None,
    ) -> Artifact:
        task = f"业务目标:{goal}\nTODO Plan:{todo_plan}\n请实现并产出结构化交付物。"
        if feedback is not None:
            task += (
                "\n\n【上一轮被打回,请针对性修复】\n"
                f"未过项/问题:{feedback.data.get('failed_checks', feedback.issues)}\n"
                f"修复建议:{feedback.data.get('suggestion', feedback.handoff_notes)}"
            )
        art = self._runner.run(state, "backend-engineer-agent", task, upstream=feedback)
        self._emit("build", status=art.status.value, files=art.artifact.files)
        return art

    def _loop_judge(
        self,
        state: CompanyState,
        goal: str,
        build: Artifact,
        acceptance: list[str],
    ) -> tuple[str, list[str], Artifact | None]:
        """F-B.7 两段判定:规则先行(硬约束,不进模型)+ 模型语义兜底。"""
        # ① 规则/可执行校验先行(M2:结构性机械校验)
        rule = run_rule_checks(build)
        if not rule.passed:
            self._emit("rule_check", passed=False, failed_checks=rule.failed_checks)
            # 硬约束不过 → 直接 reject,不进模型(省 token)
            judge = build.model_copy(deep=True)
            judge.data = {
                "verdict": "reject",
                "failed_checks": rule.failed_checks,
                "suggestion": "请修复上述硬性校验项后重试。",
            }
            return "reject", rule.failed_checks, judge
        self._emit("rule_check", passed=True, failed_checks=[])

        # ② 模型语义判定兜底
        judge_task = (
            f"业务目标:{goal}\n"
            f"验收标准:{acceptance}\n"
            "上一棒交付物已通过硬性校验。请你做语义判定:是否满足宿主意图、"
            "体验是否合理。输出 verdict=pass|reject 及理由与建议。"
        )
        judge = self._runner.run(
            state, "loop-judge-agent", judge_task, upstream=build
        )
        verdict = judge.data.get("verdict", "pass")
        failed = judge.data.get("failed_checks", [])
        return verdict, failed, judge

    def _rerun_tail(
        self,
        state: CompanyState,
        goal: str,
        todo_plan: list[dict[str, Any]],
        acceptance: list[str],
        feedback: Artifact,
    ) -> RuntimeResult:
        """验收回退后再走一轮构建→判定→验收(受 LoopController 上限保护)。"""
        build = self._build_step(state, goal, todo_plan, feedback)
        verdict, failed, judge = self._loop_judge(state, goal, build, acceptance)
        self._emit("loop_judge", verdict=verdict, failed_checks=failed,
                   iteration=self._loop.iterations(state, _LOOP_KEY))
        if decide_from_verdict(verdict) != Decision.PASS:
            return self._need_decision(
                state, note=f"重试后仍未过 Loop 判定: {failed}", final=build
            )
        summary = self._runner.run(
            state, "dev-lead-agent",
            f"业务目标:{goal}\n请基于已通过验收的交付物做最终汇总,回报 CEO。",
            upstream=build,
        )
        state.status = TaskStatus.DONE
        self._runner.checkpoint(state)
        self._emit("graph_done", task_id=state.task_id)
        return RuntimeResult(state, TaskStatus.DONE, summary,
                             "电商业务流 PoC(含验收回退)跑通", self._events)

    def _need_decision(
        self, state: CompanyState, note: str, final: Artifact | None
    ) -> RuntimeResult:
        state.status = TaskStatus.NEED_DECISION
        self._runner.checkpoint(state)
        self._emit("need_decision", note=note)
        return RuntimeResult(
            state, TaskStatus.NEED_DECISION, final, note, self._events
        )
