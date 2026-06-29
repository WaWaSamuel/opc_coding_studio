"""检索式记忆注入 + 稳定前缀缓存(F-C.5/C.6 / M05)。

Retriever(F-C.5):
  按"角色 + 任务"检索长期记忆 Top-K 注入,控 token。落点 sqlite-vec + Ark
  embedding 是可选增强;本期默认**关键词回退**(repo.search_memory),无外部
  依赖即可跑通;命名空间隔离 runtime/edit(F-C.4)由 MemoryManager 负责。

PrefixCache(F-C.6):
  把"稳定前缀(公共层+角色层+长期记忆 Top-K,即 CACHE_BOUNDARY 之前)"做
  指纹,跟踪命中率。M1 仅插 CACHE_BOUNDARY 标记;M3 在此**实际统计命中**——
  同一前缀重复出现即记一次 hit(对齐 Anthropic prompt caching:稳定前缀置顶
  命中缓存降本)。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from backend.core.memory import MemoryManager
from backend.core.roles.prompt_composer import CACHE_BOUNDARY


@dataclass
class RetrievedMemory:
    items: list[dict]

    def as_prompt_block(self) -> str:
        if not self.items:
            return ""
        lines = [f"- [{it['kind']}] {it['text']}" for it in self.items]
        return "# 长期记忆(检索 Top-K,仅供参考)\n" + "\n".join(lines)


class Retriever:
    """按角色+任务检索长期记忆 Top-K(关键词回退默认实现)。"""

    def __init__(self, memory: MemoryManager) -> None:
        self._mem = memory

    def retrieve(self, role_id: str, task_text: str, top_k: int | None = None) -> RetrievedMemory:
        query = f"{role_id} {task_text}"
        hits = self._mem.recall(query, top_k=top_k)
        return RetrievedMemory(items=hits)


@dataclass
class PrefixCache:
    """稳定前缀指纹 + 命中率统计(F-C.6 实际命中)。"""

    _seen: dict[str, int] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    @staticmethod
    def fingerprint(prefix: str) -> str:
        return hashlib.sha1(prefix.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def stable_prefix(messages: list[dict[str, str]]) -> str:
        """提取 CACHE_BOUNDARY 之前的稳定前缀(系统消息边界前部分)。"""
        system = messages[0]["content"] if messages else ""
        return system.split(CACHE_BOUNDARY, 1)[0]

    def observe(self, messages: list[dict[str, str]]) -> bool:
        """记录一次调用的前缀;返回是否命中(此前出现过同一前缀)。"""
        fp = self.fingerprint(self.stable_prefix(messages))
        if fp in self._seen:
            self._seen[fp] += 1
            self.hits += 1
            return True
        self._seen[fp] = 1
        self.misses += 1
        return False

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
