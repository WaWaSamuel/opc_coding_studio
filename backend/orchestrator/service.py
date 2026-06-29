"""OrchestratorService:M4 应用编排服务(入口层与界面共用的核心)。

把 M1~M3 的内核(Adapter/Registry/Repo/CostGuard/Checkpoint/Memory/PrefixCache)
与 M4 的 EventBus + DecisionGate + SessionRouter 组装成一个长生命周期服务对象,
对外提供:
  - submit(host_command) -> task_id:把 HostCommand 投编排器,后台线程跑 RuntimeGraph
  - subscribe(task_id) / unsubscribe:SSE/飞书消费事件流
  - decide(task_id, decision):人在环回灌唤醒(等价飞书卡片按钮)
  - task_snapshot(task_id) / cost(task_id):对齐/恢复 与 成本聚合

FastAPI 路由层(api/)和飞书长连接(gateway/lark_adapter)都只依赖这个服务,
不各自持有内核;一处装配,多渠道复用。
"""
from __future__ import annotations

import queue
import threading
import uuid
from typing import Any

from backend.config import settings
from backend.core.cost_guard import CostGuard
from backend.core.event_bus import EventBus
from backend.core.memory import MemoryManager
from backend.core.model_adapter.factory import build_adapter
from backend.core.retrieval import PrefixCache
from backend.core.roles.registry import RoleRegistry
from backend.gateway.host_command import HostCommand
from backend.gateway.session_router import HostAuthorizer, SessionRouter
from backend.orchestrator.decision_gate import Decision, DecisionGate
from backend.orchestrator.graph_edit import EditGraph
from backend.orchestrator.graph_runtime import RuntimeGraph
from backend.orchestrator.loop import LoopController
from backend.orchestrator.node_runner import NodeRunner
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState, TaskStatus
from backend.services.badcase import BadcaseCollector
from backend.services.git_service import GitService
from backend.services.scheduler import Scheduler
from backend.services.seed_testcases import load_seed_testcases
from backend.services.testsuite import TestSuiteRunner, runtime_case_runner


class OrchestratorService:
    def __init__(self, db_path: str | None = None, adapter=None) -> None:
        self._repo = SqliteRepo(db_path or settings.db_path)
        self._adapter = adapter or build_adapter()
        self._registry = RoleRegistry()
        self._cost = CostGuard(self._repo)
        self._ckpt = CheckpointStore(self._repo)
        self._bus = EventBus(self._repo)
        self._gate = DecisionGate()
        self._sessions = SessionRouter()
        self._auth = HostAuthorizer(settings.lark_bot_target_open_id)
        # M5 自迭代 + 版本管理:GitService(本地优先 + 受控 PR)、Badcase 收集、
        # TestSuiteRunner(回归用 runtime 记忆 namespace)、Scheduler(每周单测)。
        self._git = GitService()
        self._badcase = BadcaseCollector(self._repo)
        self._testsuite = TestSuiteRunner(
            self._repo,
            runtime_case_runner(lambda: self._new_runner(namespace="runtime")),
        )
        self._scheduler = Scheduler()
        # 任务级运行态(状态/线程)。落库是真源,这里只做内存索引便于查询。
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        # F-E.5:仅当显式开启(默认关)才起每周回归调度,保证离线/测试确定性。
        if settings.scheduler_enabled:
            self.start_scheduler()

    # --- 暴露给入口层 ---
    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def sessions(self) -> SessionRouter:
        return self._sessions

    @property
    def auth(self) -> HostAuthorizer:
        return self._auth

    def _new_runner(self, namespace: str = "runtime") -> NodeRunner:
        # 每个任务独立 memory namespace 复用同一 repo;前缀缓存与事件总线注入。
        # F-C.4:Runtime/Edit 记忆隔离 —— edit 任务用 namespace="edit",不串味。
        memory = MemoryManager(self._repo, namespace=namespace)
        return NodeRunner(
            self._adapter, self._registry, self._repo, self._cost, self._ckpt,
            memory=memory, prefix_cache=PrefixCache(), event_bus=self._bus,
        )

    def submit(self, cmd: HostCommand) -> str:
        """投递指令:开任务 → 后台线程跑编排图 → 立即返回 task_id。

        intent=edit → EditGraph(改系统,edit 记忆隔离);否则 RuntimeGraph(跑业务)。
        """
        if not cmd.host_verified:
            raise PermissionError("非 Host 来源,拒绝投递(F-A.1)")

        # 同一会话已有活跃任务且在等决策时,这条消息当作澄清不另开任务由调用方处理;
        # 这里默认每次 submit 开新任务(多轮对话的语义编排留 G9)。
        is_edit = cmd.intent == "edit"
        prefix = "edit" if is_edit else "task"
        task_id = self._sessions.new_task_id(cmd.session_id, prefix=prefix)
        state = CompanyState(task_id=task_id,
                             system="edit" if is_edit else "runtime",
                             workflow=f"{cmd.channel}-{cmd.intent}")
        state.payload = {"channel": cmd.channel, "session_id": cmd.session_id,
                         "reply_to": cmd.reply_to, "intent": cmd.intent}
        self._ckpt.save(state)

        def _run() -> None:
            try:
                if is_edit:
                    graph = EditGraph(
                        self._new_runner(namespace="edit"), self._git,
                        loop=LoopController(), testsuite=self._testsuite,
                        event_bus=self._bus, decision_gate=self._gate,
                    )
                else:
                    graph = RuntimeGraph(
                        self._new_runner(namespace="runtime"), loop=LoopController(),
                        event_bus=self._bus, decision_gate=self._gate,
                    )
                graph.run(state, cmd.text)
            except Exception as exc:  # noqa: BLE001 — 收口任何异常为 error 事件
                self._bus.emit(task_id, "error", role="orchestrator",
                               payload={"error": f"{type(exc).__name__}: {exc}"},
                               persist=True)
            finally:
                # 任务收口后顺手做 Badcase 沉淀(异常/回退/否决 → 入测试集)。
                try:
                    self._badcase.collect(task_id)
                except Exception:  # noqa: BLE001 — 收集失败不影响主流程
                    pass
                self._gate.clear(task_id)

        t = threading.Thread(target=_run, name=f"task-{task_id}", daemon=True)
        with self._lock:
            self._threads[task_id] = t
        t.start()
        return task_id

    def subscribe(self, task_id: str) -> queue.Queue:
        return self._bus.subscribe(task_id)

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        self._bus.unsubscribe(task_id, q)

    def decide(self, task_id: str, verdict: str, reason: str = "",
               suggestion: str = "") -> bool:
        """人在环回灌(F-A.7)。verdict ∈ pass|reject|abort。"""
        return self._gate.submit(
            task_id, Decision(verdict=verdict, reason=reason, suggestion=suggestion)
        )

    def task_snapshot(self, task_id: str) -> dict[str, Any] | None:
        state = self._ckpt.restore(task_id)
        if state is None:
            return None
        return state.model_dump(mode="json")

    def task_status(self, task_id: str) -> str | None:
        state = self._ckpt.restore(task_id)
        return state.status.value if state else None

    def cost(self, task_id: str) -> dict[str, Any]:
        """成本聚合(F-A.6):读流转日志按角色汇总 tokens/latency。"""
        logs = self._repo.logs_for(task_id)
        by_role: dict[str, dict[str, int]] = {}
        total_tokens = 0
        total_latency = 0
        for e in logs:
            role = e.get("role", "") or "unknown"
            tk = int(e.get("tokens", 0) or 0)
            lat = int(e.get("latency_ms", 0) or 0)
            slot = by_role.setdefault(role, {"tokens": 0, "latency_ms": 0, "calls": 0})
            slot["tokens"] += tk
            slot["latency_ms"] += lat
            if tk > 0:
                slot["calls"] += 1
            total_tokens += tk
            total_latency += lat
        return {
            "task_id": task_id,
            "total_tokens": total_tokens,
            "total_latency_ms": total_latency,
            "by_role": by_role,
        }

    def history(self, task_id: str) -> list[dict[str, Any]]:
        """回放:返回该任务已落库的全部流转事件(F-A.4 按 task_id 回放)。"""
        return self._repo.logs_for(task_id)

    def is_done(self, task_id: str) -> bool:
        status = self.task_status(task_id)
        return status in (TaskStatus.DONE.value, TaskStatus.FAILED.value)

    # --- Edit 系统可视化 / 版本动作(M5 / F-A.8 / F-E.3/E.4/E.5)---
    def edit_graph(self, ref: str = "main") -> dict[str, Any]:
        """Edit 工作流静态 DAG(供 GET /edit/graph 可视化)。

        feature ref 时,从 GitService 计划里推出本次涉及的改动文件,标到节点上
        做 diff 高亮(F-A.8);main ref 返回干净 DAG。Token 永不出现在返回中。
        """
        changed: list[str] = []
        if ref != self._git.main_branch:
            # 工程师节点是改动落点;有计划写盘动作即标其为 changed。
            if any(a.get("action") == "apply_changes" for a in self._git.plan):
                changed.append("edit-engineer-agent")
        spec = EditGraph.dag_spec(ref=ref, changed_targets=changed)
        spec["git"] = {
            "enabled": self._git.enabled,
            "can_push": self._git.can_push,
            "main_branch": self._git.main_branch,
        }
        return spec

    def submit_edit_pr(self, branch: str, summary: str,
                       badcase_ref: str = "") -> dict[str, Any]:
        """提 PR(供 POST /edit/pr)。受控:默认 dry-run,不擅自推远端。"""
        pr = self._git.open_pr(branch, summary, badcase_ref)
        return {
            "pr_url": pr.pr_url, "branch": pr.branch, "title": pr.title,
            "pushed": pr.pushed, "dry_run": pr.dry_run,
        }

    def run_testsuite(self, only_active: bool = True) -> dict[str, Any]:
        """跑一遍回归测试集(F-E.3);返回通过率报告。"""
        return self._testsuite.run(only_active=only_active).as_dict()

    def load_seed_testcases(self) -> int:
        """G7 测试集冷启动:载入种子用例,返回新增条数。"""
        return load_seed_testcases(self._repo)

    def start_scheduler(self) -> None:
        """启动每周 Badcase 单测调度(F-E.5)。<阈值则告警 Host(error 事件)。"""
        self._scheduler.add_job(
            name="weekly-regression",
            interval_seconds=settings.scheduler_weekly_seconds,
            func=self._weekly_regression_job,
        )
        self._scheduler.start()

    def stop_scheduler(self) -> None:
        self._scheduler.stop()

    def _weekly_regression_job(self) -> dict[str, Any]:
        """每周回归:先确保有种子用例兜底,跑回归;劣化则告警 Host。"""
        self.load_seed_testcases()
        report = self._testsuite.run()
        if report.below_threshold:
            self._bus.emit(
                "system", "error", role="scheduler",
                payload={"alert": "weekly_regression_below_threshold",
                         "pass_rate": report.pass_rate,
                         "threshold": report.threshold},
                persist=True,
            )
        return report.as_dict()

    def close(self) -> None:
        self._scheduler.stop()
        self._repo.close()
