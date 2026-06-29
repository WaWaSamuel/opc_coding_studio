"""M2 电商业务流 PoC 端到端 demo。

跑通一条真实业务流:
  CEO 路由 → 开发部长拆解(TODO+acceptance)→ 后端执行
    → Loop 判定(规则先行 + 模型语义兜底)→ 业务回退(≤3)
    → 需求验收 → 部长汇总 → done

默认 MockAdapter 跑不通完整链路(它只产单一 Artifact),因此本 demo
建议用真实 ark:MODEL_PROVIDER=ark python -m backend.run_ecommerce
"""
from __future__ import annotations

import uuid

from backend.config import settings
from backend.core.cost_guard import CostGuard
from backend.core.memory import MemoryManager
from backend.core.model_adapter.factory import build_adapter
from backend.core.retrieval import PrefixCache
from backend.core.roles.registry import RoleRegistry
from backend.orchestrator.graph_runtime import RuntimeGraph
from backend.orchestrator.loop import LoopController
from backend.orchestrator.node_runner import NodeRunner
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState

GOAL = "搭建一个最小电商下单后端:商品列表接口、下单接口、订单查询接口,给出文件清单与摘要。"


def main() -> None:
    print(f"[M2] provider={settings.model_provider} db={settings.db_path}")
    repo = SqliteRepo(settings.db_path)
    adapter = build_adapter()
    registry = RoleRegistry()
    cost = CostGuard(repo)
    ckpt = CheckpointStore(repo)
    # M3 harness 接入主链路:检索式记忆注入(F-C.5)+ 稳定前缀缓存统计(F-C.6)。
    # 节点重试(F-D.2)由 NodeRunner 内部默认启用,无需显式注入。
    memory = MemoryManager(repo, namespace="runtime")
    prefix_cache = PrefixCache()
    runner = NodeRunner(
        adapter, registry, repo, cost, ckpt,
        memory=memory, prefix_cache=prefix_cache,
    )

    print(f"[M2] 已注册角色: {registry.list_ids()}")

    def on_event(e: dict) -> None:
        ev = e.get("event")
        extra = {k: v for k, v in e.items() if k != "event"}
        print(f"   ▶ {ev:<14} {extra}")

    graph = RuntimeGraph(runner, loop=LoopController(), event_sink=on_event)

    task_id = f"ecom-{uuid.uuid4().hex[:8]}"
    state = CompanyState(task_id=task_id, workflow="ecommerce-poc")

    print(f"\n[M2] 业务流开始 task_id={task_id}")
    res = graph.run(state, GOAL)

    print(f"\n[M2] 业务流结束: status={res.status.value}")
    print(f"[M2] 备注: {res.note}")
    print(f"[M2] 任务累计 token: {state.task_tokens}")
    print(f"[M2] 业务回退计数: {state.loop_counters}")
    print(f"[M2] 节点重试计数: {state.retry_counters}")
    print(f"[M3] 前缀缓存命中率: {prefix_cache.hit_rate:.0%} "
          f"(hits={prefix_cache.hits} misses={prefix_cache.misses})")
    if res.final_artifact:
        print(f"[M2] 最终交付: files={res.final_artifact.artifact.files} "
              f"summary={res.final_artifact.artifact.summary[:60]}")

    # 断点恢复验证(F-D.4)
    restored = ckpt.restore(task_id)
    assert restored is not None
    print(f"[M2] 断点恢复: artifacts={len(restored.artifacts)} "
          f"status={restored.status.value}")

    logs = repo.logs_for(task_id)
    print(f"[M2] 流转日志 {len(logs)} 条")

    repo.close()
    print("\n[M2] 电商业务流 PoC 完成 ✓")


if __name__ == "__main__":
    main()
