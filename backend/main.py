"""M1 最小内核闭环演示。

跑通:单角色被调一次 → 产出合法 Artifact → 落 Checkpoint → 断点可恢复
     → token 被记账(可触发熔断)。

默认 MockAdapter(离线,无需密钥);MODEL_PROVIDER=ark 接真实豆包。
运行(仓库根目录): python -m backend.main
"""
from __future__ import annotations

import uuid

from backend.config import settings
from backend.core.cost_guard import CostGuard
from backend.core.model_adapter.factory import build_adapter
from backend.core.roles.registry import RoleRegistry
from backend.orchestrator.node_runner import NodeRunner
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState


def main() -> None:
    print(f"[M1] provider={settings.model_provider} db={settings.db_path}")

    repo = SqliteRepo(settings.db_path)
    adapter = build_adapter()
    registry = RoleRegistry()
    cost = CostGuard(repo)
    ckpt = CheckpointStore(repo)
    runner = NodeRunner(adapter, registry, repo, cost, ckpt)

    print(f"[M1] 已注册角色: {registry.list_ids()}")

    task_id = f"demo-{uuid.uuid4().hex[:8]}"
    state = CompanyState(task_id=task_id, workflow="m1-smoke")

    artifact = runner.run(
        state,
        role_id="backend-engineer-agent",
        task_text="实现一个返回当前时间的 HTTP 接口,给出文件清单与摘要。",
    )

    print(f"\n[M1] 产出 Artifact: status={artifact.status.value} "
          f"files={artifact.artifact.files}")
    print(f"[M1] 任务累计 token: {state.task_tokens}")

    # 断点恢复演示(F-D.4)
    restored = ckpt.restore(task_id)
    assert restored is not None, "checkpoint 恢复失败"
    assert restored.artifacts and restored.artifacts[-1].status == artifact.status
    print(f"[M1] 断点恢复成功: task_id={restored.task_id} "
          f"artifacts={len(restored.artifacts)} transition='{restored.transition}'")

    # 流转日志(F-A.6)
    logs = repo.logs_for(task_id)
    print(f"[M1] 流转日志 {len(logs)} 条:")
    for e in logs:
        print(f"   - {e['event']:<16} role={e['role']} tokens={e['tokens']} "
              f"task_tokens={e['task_tokens']}")

    repo.close()
    print("\n[M1] 闭环完成 ✓")


if __name__ == "__main__":
    main()
