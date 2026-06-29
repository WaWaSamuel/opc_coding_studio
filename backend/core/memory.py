"""Context & Memory:三层记忆 + 三级上下文流水线(M05 / F-C.2/C.3)。

借 Claude Code "上下文管理是三级流水线而非单次操作":

三层记忆(F-C.2):
  - WorkingMem  工作记忆:本轮调用的即时上下文(短命,内存)
  - TaskMem     任务记忆:本任务全过程的结构化条目(随任务存活)
  - LongTermMem 长期记忆:跨任务沉淀,命名空间隔离 runtime/edit(F-C.4),落库

三级流水线(每次调用前依次执行,F-C.3):
  ① ResultBudgeter:大产出落 DB,prompt 内只留 ≤2KB 预览 + 指针,
     按需 fetch_artifact 取回(demand-paging)
  ② MicroCompactor:按来源选择性清旧结果(以"缓存过期/超条数"为触发),
     轻量、不调模型
  ③ Compactor:超阈值时一次 LLM 结构化**九段摘要**(含 Host 原话逐字引用),
     替换原文但保留可追溯指针。连续失败计入熔断(MAX_COMPACT_FAILURES),
     超限保守保留原文并告警(借 Claude Code circuit breaker)。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.config import settings
from backend.errors import OpcError
from backend.repo.repository import Repository

# 全量压缩的九段结构化摘要 schema(借 Claude Code 九段 + Host 原话锚点)
COMPACT_SECTIONS = [
    "primary_request_and_intent",   # 含 Host 原话逐字引用,防意图漂移
    "key_technical_concepts",
    "files_and_artifacts",
    "errors_and_fixes",
    "problem_solving",
    "pending_tasks",
    "current_work",
    "optional_next_step",           # 要求逐字引用 Host 原话锚点
    "traceable_pointers",           # 指向 DB 完整 Artifact 的 ref,可追溯非截断
]


class CompactCircuitOpen(OpcError):
    """连续 Compact 失败达上限,熔断:停压缩,保守保留原文并告警 Host。"""


@dataclass
class MemoryEntry:
    kind: str               # host_message / artifact / decision / summary ...
    text: str
    ref: str | None = None  # 落库大产出的指针(demand-paging)


@dataclass
class WorkingMem:
    """工作记忆:单轮即时上下文,最短命。"""
    items: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.items.append(text)

    def clear(self) -> None:
        self.items.clear()


class ResultBudgeter:
    """三级 ①:大产出落 DB,prompt 内留预览 + 指针。"""

    def __init__(self, repo: Repository, preview_bytes: int | None = None) -> None:
        self._repo = repo
        self._preview = preview_bytes or settings.result_preview_bytes

    def budget(self, task_id: str, kind: str, content: str) -> MemoryEntry:
        raw = content.encode("utf-8")
        if len(raw) <= self._preview:
            return MemoryEntry(kind=kind, text=content)
        # 超预算:落库 + 留预览 + 指针
        ref = "art-" + hashlib.sha1(
            (task_id + kind + content).encode("utf-8")
        ).hexdigest()[:16]
        self._repo.save_artifact(task_id, ref, content)
        preview = raw[: self._preview].decode("utf-8", errors="ignore")
        return MemoryEntry(
            kind=kind,
            text=f"{preview}\n…[已截断,完整内容见指针 {ref},按需 fetch]",
            ref=ref,
        )

    def fetch(self, ref: str) -> str | None:
        return self._repo.load_artifact(ref)


class MicroCompactor:
    """三级 ②:按来源选择性清旧结果(轻量,不调模型)。

    触发:任务记忆条目数超 keep_recent 时,清掉**较旧的可重取产出**
    (有 ref 指针、可按需取回的),保留最近 N 条与所有无指针的关键条目。
    """

    def __init__(self, keep_recent: int = 12) -> None:
        self._keep = keep_recent

    def compact(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        if len(entries) <= self._keep:
            return entries
        head = entries[: -self._keep]
        tail = entries[-self._keep :]
        pruned: list[MemoryEntry] = []
        for e in head:
            if e.ref is not None:
                # 可重取:压成一行指针占位,需要时 fetch 取回
                pruned.append(MemoryEntry(
                    kind=e.kind,
                    text=f"[旧产出已清,指针 {e.ref} 可取回]",
                    ref=e.ref,
                ))
            else:
                pruned.append(e)  # 无指针的关键条目保留
        return pruned + tail


CompactInvoker = Callable[[str], str]  # (prompt) -> 模型返回的九段 JSON 文本


class Compactor:
    """三级 ③:全量 LLM 结构化九段摘要(含 Host 原话锚点)+ 熔断。"""

    def __init__(self, max_failures: int | None = None) -> None:
        self._max_failures = max_failures or settings.max_compact_failures
        self._consecutive_failures = 0

    @property
    def failures(self) -> int:
        return self._consecutive_failures

    def compact(
        self,
        entries: list[MemoryEntry],
        host_message: str,
        invoke: CompactInvoker,
    ) -> dict[str, Any]:
        """超阈值时调模型产九段摘要;失败计入熔断,超限抛 CompactCircuitOpen。"""
        if self._consecutive_failures >= self._max_failures:
            raise CompactCircuitOpen(
                f"连续压缩失败 {self._consecutive_failures} 次达上限,停压缩保留原文"
            )
        refs = [e.ref for e in entries if e.ref]
        corpus = "\n".join(f"[{e.kind}] {e.text}" for e in entries)
        prompt = (
            "请把下方任务历史压缩为结构化九段摘要,严格输出一个 JSON 对象,"
            f"字段为:{COMPACT_SECTIONS}。\n"
            f"约束:`primary_request_and_intent` 与 `optional_next_step` 必须"
            f"**逐字引用** Host 原话锚点:「{host_message}」,防意图漂移;"
            f"`traceable_pointers` 必须包含可追溯指针:{refs}。\n\n"
            f"# 任务历史\n{corpus}"
        )
        try:
            raw = invoke(prompt)
            data = json.loads(_strip_fence(raw))
        except Exception as exc:  # noqa: BLE001 — 任何失败都计入熔断
            self._consecutive_failures += 1
            raise CompactCircuitOpen(
                f"压缩失败(第 {self._consecutive_failures} 次): {exc}"
            ) from exc
        self._consecutive_failures = 0  # 成功复位
        # 兜底:保证 host 锚点与指针一定在产物里(非截断、可追溯)
        data.setdefault("optional_next_step", "")
        data["host_anchor"] = host_message
        data["traceable_pointers"] = list(
            dict.fromkeys(list(data.get("traceable_pointers", [])) + refs)
        )
        return data


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    return t.strip()


class MemoryManager:
    """编排三层记忆 + 三级流水线;runtime/edit 命名空间隔离(F-C.4)。"""

    def __init__(
        self,
        repo: Repository,
        namespace: str = "runtime",
        budgeter: ResultBudgeter | None = None,
        micro: MicroCompactor | None = None,
        compactor: Compactor | None = None,
    ) -> None:
        self._repo = repo
        self._ns = namespace
        self._budgeter = budgeter or ResultBudgeter(repo)
        self._micro = micro or MicroCompactor()
        self._compactor = compactor or Compactor()
        self.task_mem: list[MemoryEntry] = []
        self.working = WorkingMem()

    # --- 任务记忆写入(经 ① 结果预算)---
    def record(self, task_id: str, kind: str, content: str) -> MemoryEntry:
        entry = self._budgeter.budget(task_id, kind, content)
        self.task_mem.append(entry)
        return entry

    def fetch(self, ref: str) -> str | None:
        """按指针取回完整产出(demand-paging)。"""
        return self._budgeter.fetch(ref)

    # --- ② 微压缩(轻量,定期/超条数触发)---
    def micro_compact(self) -> None:
        self.task_mem = self._micro.compact(self.task_mem)

    # --- ③ 全量压缩(超 token 阈值触发)---
    def should_full_compact(self) -> bool:
        approx_tokens = sum(len(e.text) for e in self.task_mem) // 2
        return approx_tokens >= settings.compact_trigger_tokens

    def full_compact(self, host_message: str, invoke: CompactInvoker) -> dict[str, Any]:
        summary = self._compactor.compact(self.task_mem, host_message, invoke)
        # 压缩后:任务记忆替换为一条摘要条目(保留指针,可追溯)
        self.task_mem = [MemoryEntry(
            kind="summary",
            text=json.dumps(summary, ensure_ascii=False),
        )]
        return summary

    # --- 长期记忆(命名空间隔离落库 + 检索)---
    def remember(self, kind: str, text: str) -> None:
        self._repo.save_memory(self._ns, kind, text)

    def recall(self, query: str, top_k: int | None = None) -> list[dict[str, Any]]:
        return self._repo.search_memory(
            self._ns, query, top_k or settings.retrieve_top_k
        )

    @property
    def compact_failures(self) -> int:
        return self._compactor.failures
