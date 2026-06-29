"""DecisionGate:人在环决策回灌(F-A.7 / M02 需求)。

编排线程在 need_decision 处阻塞等 Host 拍板;Host 经 POST /decision 或
飞书交互卡片按钮回调 submit 决策,唤醒对应任务线程继续。

线程模型:每个等待中的 task_id 持有一个 threading.Event + 结果槽。
超时(decision_timeout_seconds)未回灌则返回 None,编排器保守收口。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from backend.config import settings


@dataclass
class Decision:
    verdict: str            # pass | reject | abort
    reason: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason,
                "suggestion": self.suggestion}


class DecisionGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, Decision] = {}

    def wait(self, task_id: str, timeout: float | None = None) -> Decision | None:
        """阻塞当前(编排)线程等待 Host 决策;超时返回 None。"""
        with self._lock:
            ev = self._events.get(task_id)
            if ev is None:
                ev = threading.Event()
                self._events[task_id] = ev
        timeout = settings.decision_timeout_seconds if timeout is None else timeout
        got = ev.wait(timeout)
        if not got:
            return None
        with self._lock:
            return self._results.pop(task_id, None)

    def submit(self, task_id: str, decision: Decision) -> bool:
        """Host 回灌决策;若该任务正在等待则唤醒返回 True。"""
        with self._lock:
            ev = self._events.get(task_id)
            self._results[task_id] = decision
            if ev is None:
                # 任务尚未进入等待:预存决策,wait 时仍需等待 Event,故也置位
                ev = threading.Event()
                self._events[task_id] = ev
            ev.set()
        return True

    def is_waiting(self, task_id: str) -> bool:
        with self._lock:
            ev = self._events.get(task_id)
            return ev is not None and not ev.is_set()

    def clear(self, task_id: str) -> None:
        with self._lock:
            self._events.pop(task_id, None)
            self._results.pop(task_id, None)
