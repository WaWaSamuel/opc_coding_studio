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
from backend.orchestrator.graph_runtime import RuntimeGraph
from backend.orchestrator.loop import LoopController
from backend.orchestrator.node_runner import NodeRunner
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState, TaskStatus


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
        # 任务级运行态(状态/线程)。落库是真源,这里只做内存索引便于查询。
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

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

    def _new_runner(self) -> NodeRunner:
        # 每个任务独立 memory namespace 复用同一 repo;前缀缓存与事件总线注入。
        memory = MemoryManager(self._repo, namespace="runtime")
        return NodeRunner(
            self._adapter, self._registry, self._repo, self._cost, self._ckpt,
            memory=memory, prefix_cache=PrefixCache(), event_bus=self._bus,
        )

    def submit(self, cmd: HostCommand) -> str:
        """投递指令:开任务 → 后台线程跑 RuntimeGraph → 立即返回 task_id。"""
        if not cmd.host_verified:
            raise PermissionError("非 Host 来源,拒绝投递(F-A.1)")

        # 同一会话已有活跃任务且在等决策时,这条消息当作澄清不另开任务由调用方处理;
        # 这里默认每次 submit 开新任务(多轮对话的语义编排留 G9)。
        prefix = "edit" if cmd.intent == "edit" else "task"
        task_id = self._sessions.new_task_id(cmd.session_id, prefix=prefix)
        state = CompanyState(task_id=task_id, workflow=f"{cmd.channel}-{cmd.intent}")
        state.payload = {"channel": cmd.channel, "session_id": cmd.session_id,
                         "reply_to": cmd.reply_to, "intent": cmd.intent}
        self._ckpt.save(state)

        def _run() -> None:
            graph = RuntimeGraph(
                self._new_runner(), loop=LoopController(),
                event_bus=self._bus, decision_gate=self._gate,
            )
            try:
                graph.run(state, cmd.text)
            except Exception as exc:  # noqa: BLE001 — 收口任何异常为 error 事件
                self._bus.emit(task_id, "error", role="orchestrator",
                               payload={"error": f"{type(exc).__name__}: {exc}"},
                               persist=True)
            finally:
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

    def close(self) -> None:
        self._repo.close()
