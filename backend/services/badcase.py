"""BadcaseCollector:监听日志异常并结构化为测试用例(M10 / F-E.2)。

监听信号(PRD):rework 频发 / loop_reject / Host 否决 / error。命中则把该任务
结构化为一条回归用例入测试集(testcases 表),供每周单测复跑、防止劣化复发。

只依赖 Repository:扫流转日志(真源)+ 取 Checkpoint 还原目标与验收标准。
入库按 task_id 去重(一个出问题的任务沉淀一条 badcase 用例)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from backend.repo.repository import Repository

# rework 多少次算"频发"
_REWORK_FREQUENT = 2


@dataclass
class BadcaseSignal:
    task_id: str
    reasons: list[str]
    goal: str
    acceptance: list[str]
    intent: str = "runtime"

    @property
    def hit(self) -> bool:
        return bool(self.reasons)


class BadcaseCollector:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    # --- 信号检测 ---
    def inspect_task(self, task_id: str) -> BadcaseSignal:
        logs = self._repo.logs_for(task_id)
        reasons: list[str] = []
        rework = 0
        for e in logs:
            event = e.get("event", "")
            note = e.get("note", "") or ""
            if event == "rework":
                rework += 1
            elif event == "error":
                reasons.append("error")
            elif event == "loop_judge" and '"verdict": "reject"' in note:
                if "loop_reject" not in reasons:
                    reasons.append("loop_reject")
            elif event == "decision":
                verdict = self._verdict_of(note)
                if verdict in ("reject", "abort"):
                    reasons.append(f"host_{verdict}")
        if rework >= _REWORK_FREQUENT:
            reasons.append(f"rework_frequent({rework})")

        goal, acceptance, intent = self._context_of(task_id, logs)
        # 去重并保持顺序
        reasons = list(dict.fromkeys(reasons))
        return BadcaseSignal(task_id, reasons, goal, acceptance, intent)

    # --- 入库 ---
    def collect(self, task_id: str) -> int | None:
        """命中异常则入库一条用例,返回行 id;未命中返回 None。"""
        sig = self.inspect_task(task_id)
        if not sig.hit or not sig.goal:
            return None
        dedup_key = f"badcase:{task_id}"
        if self._repo.has_testcase(dedup_key):
            return None
        return self._repo.save_testcase({
            "dedup_key": dedup_key,
            "source": "badcase",
            "intent": sig.intent,
            "goal": sig.goal,
            "acceptance": sig.acceptance,
            "origin_task": task_id,
        })

    def scan_all(self, task_ids: list[str]) -> list[int]:
        added: list[int] = []
        for tid in task_ids:
            rid = self.collect(tid)
            if rid:
                added.append(rid)
        return added

    # --- 辅助 ---
    @staticmethod
    def _verdict_of(note: str) -> str:
        try:
            data = json.loads(note)
            return str(data.get("verdict", ""))
        except (TypeError, ValueError):
            return ""

    def _context_of(
        self, task_id: str, logs: list[dict[str, Any]]
    ) -> tuple[str, list[str], str]:
        """从 graph_start 取 goal,从 Checkpoint 还原 acceptance / intent。"""
        goal = ""
        for e in logs:
            if e.get("event") == "graph_start":
                try:
                    goal = json.loads(e.get("note", "{}")).get("goal", "")
                except (TypeError, ValueError):
                    goal = ""
                break
        acceptance: list[str] = []
        intent = "runtime"
        state = self._repo.load_checkpoint(task_id)
        if state is not None:
            intent = state.payload.get("intent", "runtime") or "runtime"
            if not goal:
                goal = state.payload.get("goal", "") or state.workflow
            for art in state.artifacts:
                acc = art.data.get("acceptance")
                if acc:
                    acceptance = list(acc)
        return goal, acceptance, intent
