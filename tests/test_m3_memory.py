"""M3 三层记忆 + 三级 Compact 流水线(F-C.2/C.3 / M05)。

验证:
  - ① ResultBudgeter:小产出原样留;大产出落库 + 留预览 + 指针,按需取回
  - ② MicroCompactor:超条数清旧的可重取产出,保留最近 + 无指针关键条目
  - ③ Compactor:产九段摘要,含 Host 原话锚点 + 可追溯指针
  - Compact 连续失败计入熔断,达上限抛 CompactCircuitOpen(保守保留原文)
  - 长期记忆命名空间隔离(runtime/edit 互不串味)
"""
from __future__ import annotations

import json

import pytest

from backend.core.memory import (
    Compactor,
    CompactCircuitOpen,
    MemoryEntry,
    MemoryManager,
    MicroCompactor,
    ResultBudgeter,
)
from backend.repo.sqlite_repo import SqliteRepo


@pytest.fixture()
def repo(tmp_path):
    r = SqliteRepo(str(tmp_path / "m3mem.db"))
    yield r
    r.close()


def test_result_budgeter_small_passthrough(repo):
    b = ResultBudgeter(repo, preview_bytes=2048)
    e = b.budget("t1", "artifact", "短产出")
    assert e.ref is None
    assert e.text == "短产出"


def test_result_budgeter_large_offloads_with_pointer(repo):
    b = ResultBudgeter(repo, preview_bytes=64)
    big = "X" * 5000
    e = b.budget("t1", "artifact", big)
    assert e.ref is not None
    assert "fetch" in e.text and len(e.text) < 5000
    # 按指针取回完整内容
    assert b.fetch(e.ref) == big


def test_micro_compactor_prunes_old_retrievable(repo):
    mc = MicroCompactor(keep_recent=3)
    entries = [MemoryEntry("artifact", f"old{i}", ref=f"r{i}") for i in range(5)]
    entries += [MemoryEntry("decision", "keep-no-ref")]  # 无指针关键条目
    entries += [MemoryEntry("artifact", "recent", ref="rN")]
    out = mc.compact(entries)
    # 旧的可重取产出被压成指针占位
    assert any("可取回" in e.text for e in out)
    # 最近 3 条保留原文
    assert out[-1].text == "recent"


def test_compactor_nine_sections_with_host_anchor(repo):
    def fake_invoke(prompt: str) -> str:
        # 模型返回九段摘要(简化)
        assert "逐字引用" in prompt  # 提示词要求引用 Host 原话
        return json.dumps({
            "primary_request_and_intent": "继续 M3",
            "key_technical_concepts": "记忆/压缩",
            "files_and_artifacts": "memory.py",
            "errors_and_fixes": "无",
            "problem_solving": "略",
            "pending_tasks": "检索注入",
            "current_work": "Compact",
            "optional_next_step": "继续下一步",
            "traceable_pointers": ["r1"],
        }, ensure_ascii=False)

    c = Compactor()
    entries = [MemoryEntry("artifact", "x", ref="r1"),
               MemoryEntry("host_message", "继续下一步")]
    summary = c.compact(entries, host_message="继续下一步", invoke=fake_invoke)
    assert summary["host_anchor"] == "继续下一步"
    assert "r1" in summary["traceable_pointers"]
    assert c.failures == 0


def test_compactor_circuit_breaker(repo):
    def bad_invoke(prompt: str) -> str:
        return "这不是合法 JSON"

    c = Compactor(max_failures=3)
    entries = [MemoryEntry("artifact", "x")]
    # 前 3 次失败累计,第 4 次直接熔断(不再调模型)
    for _ in range(3):
        with pytest.raises(CompactCircuitOpen):
            c.compact(entries, "host", bad_invoke)
    assert c.failures == 3
    with pytest.raises(CompactCircuitOpen):
        c.compact(entries, "host", bad_invoke)  # 熔断态


def test_long_term_memory_namespace_isolation(repo):
    rt = MemoryManager(repo, namespace="runtime")
    ed = MemoryManager(repo, namespace="edit")
    rt.remember("decision", "电商下单用 FastAPI")
    ed.remember("decision", "Edit 改 prompt 流程")
    # runtime 检索不应命中 edit 的记忆
    rt_hits = rt.recall("FastAPI")
    assert any("FastAPI" in h["text"] for h in rt_hits)
    assert all("Edit" not in h["text"] for h in rt_hits)
    ed_hits = ed.recall("Edit")
    assert any("Edit" in h["text"] for h in ed_hits)


def test_memory_manager_record_and_fetch(repo):
    mm = MemoryManager(repo, namespace="runtime",
                       budgeter=ResultBudgeter(repo, preview_bytes=32))
    e = mm.record("t1", "artifact", "Y" * 1000)
    assert e.ref is not None
    assert mm.fetch(e.ref) == "Y" * 1000
    assert len(mm.task_mem) == 1
