"""M3 检索注入 + 稳定前缀缓存(F-C.5/C.6 / M05)。

验证:
  - Retriever 按角色+任务检索长期记忆 Top-K(关键词回退)
  - 检索结果注入到 CACHE_BOUNDARY **之前**(稳定前缀区)
  - PrefixCache 命中统计:同一稳定前缀重复 → hit;不同 → miss
  - 命中率计算正确
"""
from __future__ import annotations

import pytest

from backend.core.memory import MemoryManager
from backend.core.retrieval import PrefixCache, Retriever
from backend.core.roles.prompt_composer import CACHE_BOUNDARY, PromptComposer
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import RoleSpec


@pytest.fixture()
def repo(tmp_path):
    r = SqliteRepo(str(tmp_path / "m3retr.db"))
    yield r
    r.close()


def test_retriever_top_k_keyword_fallback(repo):
    mm = MemoryManager(repo, namespace="runtime")
    mm.remember("decision", "订单接口用 FastAPI 实现")
    mm.remember("decision", "前端用 React")
    mm.remember("lesson", "数据库选 SQLite")
    rv = Retriever(mm)
    res = rv.retrieve("backend-engineer-agent", "实现订单 FastAPI 接口", top_k=2)
    assert len(res.items) <= 2
    # 最相关(FastAPI/订单)应排前
    assert "FastAPI" in res.items[0]["text"]


def test_retrieval_injected_before_cache_boundary(repo):
    mm = MemoryManager(repo, namespace="runtime")
    mm.remember("decision", "订单接口用 FastAPI 实现")
    rv = Retriever(mm)
    block = rv.retrieve("backend-engineer-agent", "实现订单接口").as_prompt_block()
    role = RoleSpec(role_id="backend-engineer-agent",
                    common_prompt="公共", role_prompt="后端")
    messages = PromptComposer.compose(role, "实现订单接口", memory_block=block)
    system = messages[0]["content"]
    before, after = system.split(CACHE_BOUNDARY, 1)
    # 记忆注入在边界前(稳定前缀区)
    assert "长期记忆" in before
    assert "FastAPI" in before
    assert "长期记忆" not in after


def test_prefix_cache_hit_and_miss():
    cache = PrefixCache()
    role = RoleSpec(role_id="r", common_prompt="公共稳定", role_prompt="角色稳定")
    m1 = PromptComposer.compose(role, "任务A")
    m2 = PromptComposer.compose(role, "任务B")  # 同前缀,不同任务
    assert cache.observe(m1) is False  # 首次 miss
    assert cache.observe(m2) is True   # 同稳定前缀 → hit
    assert cache.hits == 1 and cache.misses == 1
    assert cache.hit_rate == 0.5


def test_prefix_cache_distinguishes_roles():
    cache = PrefixCache()
    r1 = RoleSpec(role_id="a", common_prompt="公共", role_prompt="角色A")
    r2 = RoleSpec(role_id="b", common_prompt="公共", role_prompt="角色B")
    cache.observe(PromptComposer.compose(r1, "x"))
    cache.observe(PromptComposer.compose(r2, "x"))  # 不同角色 → 不同前缀
    assert cache.misses == 2 and cache.hits == 0


def test_memory_block_changes_prefix(repo):
    """记忆注入在边界前,变更记忆会改变稳定前缀(缓存语义正确)。"""
    cache = PrefixCache()
    role = RoleSpec(role_id="r", common_prompt="公共", role_prompt="角色")
    m_no_mem = PromptComposer.compose(role, "任务")
    m_with_mem = PromptComposer.compose(role, "任务", memory_block="# 长期记忆\n- x")
    cache.observe(m_no_mem)
    cache.observe(m_with_mem)
    assert cache.misses == 2  # 注入记忆后前缀不同
