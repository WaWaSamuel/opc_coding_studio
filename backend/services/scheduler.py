"""Scheduler:定时调度器(M10 / F-E.5)。

职责:① 业务定时任务;② 每周 Badcase 单测(对测试集跑回归,<阈值告警 Host)。

实现取舍:PRD 指定 APScheduler;本期不引该依赖(保持离线/轻量),用标准库
threading.Timer 重复触发实现等价语义,接口对齐(add_job / start / stop),
生产可平滑替换为 APScheduler。每个 job 独立线程触发,异常被收口不打断调度。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Job:
    name: str
    interval_seconds: float
    func: Callable[[], Any]
    run_immediately: bool = False
    runs: int = 0
    last_result: Any = None
    last_error: str = ""


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._running = False

    def add_job(
        self,
        name: str,
        interval_seconds: float,
        func: Callable[[], Any],
        run_immediately: bool = False,
    ) -> None:
        with self._lock:
            self._jobs[name] = Job(name, interval_seconds, func, run_immediately)

    def start(self) -> None:
        with self._lock:
            self._running = True
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.run_immediately:
                self._fire(job.name)
            else:
                self._arm(job)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            timers = list(self._timers.values())
            self._timers.clear()
        for t in timers:
            t.cancel()

    def run_job_now(self, name: str) -> Any:
        """手动立即触发一个 job 并返回其结果(供测试/Host 手动跑)。"""
        return self._invoke(name)

    # --- 内部 ---
    def _arm(self, job: Job) -> None:
        if job.interval_seconds <= 0:
            return
        timer = threading.Timer(job.interval_seconds, self._fire, args=(job.name,))
        timer.daemon = True
        with self._lock:
            if not self._running:
                return
            self._timers[job.name] = timer
        timer.start()

    def _fire(self, name: str) -> None:
        self._invoke(name)
        with self._lock:
            job = self._jobs.get(name)
            running = self._running
        if job is not None and running:
            self._arm(job)  # 周期性重新计时

    def _invoke(self, name: str) -> Any:
        with self._lock:
            job = self._jobs.get(name)
        if job is None:
            return None
        try:
            job.last_result = job.func()
            job.last_error = ""
        except Exception as exc:  # noqa: BLE001 — job 失败不打断调度
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.last_result = None
        finally:
            job.runs += 1
        return job.last_result

    def job_status(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(name)
        if job is None:
            return None
        return {
            "name": job.name, "interval_seconds": job.interval_seconds,
            "runs": job.runs, "last_error": job.last_error,
            "last_result": job.last_result,
        }
