"""EventBus:统一事件流总线(M11 / F-A.4 可观测)。

编排器关键节点把事件推到这里,EventBus 同时:
  ① 落库(append_log,审计与成本聚合的真源,F-A.6)
  ② 内存 pub/sub 推给订阅者(SSE/WS → 界面;飞书卡片流式更新)

统一事件信封(对齐 PRD 5.5 SSE/WS 契约):
  Event = { task_id, ts, event, role, payload, tokens{in,out}, latency_ms }
事件类型(F-A.4):
  role_start / thinking / tool_call / artifact / handoff
  / rework / need_decision / error / done

线程安全:并行节点(F-B.1)从多线程发事件;订阅队列用 queue.Queue,
publish 用一把锁保护订阅者表的增删。
"""
from __future__ import annotations

import queue
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.repo.repository import Repository

# F-A.4 事件类型白名单(条件边/界面按此渲染)
EVENT_TYPES = frozenset({
    "graph_start", "role_start", "thinking", "tool_call", "artifact",
    "handoff", "rework", "need_decision", "decision", "error", "done",
    # M2 编排细分事件(向后兼容 graph_runtime 已有 _emit)
    "ceo_route", "dev_plan", "build", "rule_check", "loop_judge", "acceptance",
    "cost_soft_limit", "node_retry",
})

# 标记订阅流结束的哨兵
_DONE_SENTINEL = object()


@dataclass
class Event:
    task_id: str
    event: str
    role: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0})
    latency_ms: int = 0
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["ts"]:
            d["ts"] = datetime.now(timezone.utc).isoformat()
        return d


class EventBus:
    """内存 pub/sub + 落库;按 task_id 分流给订阅者。"""

    def __init__(self, repo: Repository | None = None) -> None:
        self._repo = repo
        self._lock = threading.Lock()
        # task_id -> 订阅该任务的队列列表(一个 SSE 连接一个队列)
        self._subs: dict[str, list[queue.Queue]] = {}

    # --- 发布 ---
    def publish(self, event: Event, persist: bool = True) -> dict[str, Any]:
        data = event.to_dict()
        # ① 落库(审计 + 成本聚合真源);persist=False 时只推流不落库
        # (节点级事件已由 NodeRunner 直接写 logs,避免重复落库)。
        if persist and self._repo is not None:
            self._repo.append_log({
                "ts": data["ts"],
                "task_id": data["task_id"],
                "system": "runtime",
                "role": data["role"],
                "event": data["event"],
                "tokens": data["tokens"]["in"] + data["tokens"]["out"],
                "latency_ms": data["latency_ms"],
                "note": _short_note(data["payload"]),
            })
        # ② 推订阅者
        with self._lock:
            subs = list(self._subs.get(data["task_id"], ()))
        for q in subs:
            q.put(data)
        # done/error 收尾:通知订阅流结束
        if data["event"] in ("done", "error"):
            for q in subs:
                q.put(_DONE_SENTINEL)
        return data

    def emit(
        self,
        task_id: str,
        event: str,
        role: str = "",
        payload: dict[str, Any] | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
        persist: bool = True,
    ) -> dict[str, Any]:
        """便捷发布入口。"""
        return self.publish(Event(
            task_id=task_id,
            event=event,
            role=role,
            payload=payload or {},
            tokens={"in": tokens_in, "out": tokens_out},
            latency_ms=latency_ms,
        ), persist=persist)

    # --- 订阅(SSE/WS 消费)---
    def subscribe(self, task_id: str, maxsize: int = 1000) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(task_id)
            if subs and q in subs:
                subs.remove(q)
                if not subs:
                    self._subs.pop(task_id, None)

    @staticmethod
    def is_sentinel(item: Any) -> bool:
        return item is _DONE_SENTINEL


def _short_note(payload: dict[str, Any], limit: int = 300) -> str:
    import json

    try:
        s = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(payload)
    return s[:limit]
