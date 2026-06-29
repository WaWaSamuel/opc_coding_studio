"""M3 Harness 加固能力离线 demo(默认 MockAdapter,无需密钥)。

逐项演示 M3 把"价值跑通"加固为"可长期运行"的四块保命/降本能力:
  ① 节点重试(F-D.2):瞬时失败(模型 5xx)原地重试,与业务回退独立计数。
  ② Tool 四层授权(F-D.5):白名单拦截 → Hook → 危险检测 → Host 二次确认。
  ③ 无依赖节点并行(F-B.1):前后端并行执行,确定性 join 回主 state。
  ④ 三层记忆 + 三级 Compact(F-C.2/C.3):大产出落库留指针(demand-paging)。

运行(仓库根目录):  python -m backend.run_m3_harness
"""
from __future__ import annotations

import json
import uuid

from backend.core.cost_guard import CostGuard
from backend.core.memory import MemoryManager
from backend.core.model_adapter.mock_adapter import MockAdapter
from backend.core.retrieval import PrefixCache, Retriever
from backend.core.roles.registry import RoleRegistry
from backend.errors import ModelCallError
from backend.orchestrator.node_runner import NodeRunner
from backend.orchestrator.parallel import ParallelExecutor, ParallelJob
from backend.orchestrator.tools import (
    DangerDetector,
    Tool,
    ToolInvoker,
    ToolNeedsConfirmation,
    ToolPermissionDenied,
    ToolRegistry,
)
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.sqlite_repo import SqliteRepo
from backend.schema import CompanyState


def _artifact_json(role: str, summary: str, files: list[str]) -> str:
    return json.dumps(
        {
            "role": role,
            "task_id": "m3-demo",
            "status": "done",
            "artifact": {"files": files, "summary": summary},
            "handoff_notes": "",
            "issues": [],
            "open_questions": [],
        },
        ensure_ascii=False,
    )


class _FlakyAdapter(MockAdapter):
    """前 fail_times 次抛 5xx,之后返回合法 Artifact(演示节点重试救回)。"""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self.attempts = 0

    def invoke(self, messages, schema=None, tier="large"):
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise ModelCallError(f"模拟 5xx 第 {self.attempts} 次")
        return super().invoke(messages, schema, tier)


def _role_responder(role_each: dict[str, str]):
    """按 system 提示词里出现的 role_id 返回对应角色的产出。"""

    def responder(messages):
        sys = messages[0]["content"]
        for rid, summary in role_each.items():
            if rid in sys:
                return _artifact_json(rid, summary, [f"{rid}.py"])
        return _artifact_json("backend-engineer-agent", "默认产出", ["a.py"])

    return responder


def demo_node_retry(repo, registry) -> None:
    print("\n── ① 节点重试(F-D.2):瞬时 5xx 原地重试救回 ──────────────")
    adapter = _FlakyAdapter(fail_times=2)  # 前 2 次失败,第 3 次成功
    runner = NodeRunner(adapter, registry, repo, CostGuard(repo), CheckpointStore(repo))
    state = CompanyState(task_id=f"retry-{uuid.uuid4().hex[:6]}")
    art = runner.run(state, "backend-engineer-agent", "实现一个接口")
    print(f"   模型实际被调 {adapter.attempts} 次 → 最终 status={art.status.value}")
    print(f"   节点重试计数 retry_counters={state.retry_counters}")
    print(f"   业务回退计数 loop_counters={state.loop_counters}  ← 两类计数独立,互不串")


def demo_tool_authz() -> None:
    print("\n── ② Tool 四层授权(F-D.5):白名单 / 危险检测 / Host 确认 ──────")
    audit: list[dict] = []
    reg = ToolRegistry()
    reg.register(Tool("fs.write", ["fs"], lambda a: f"写入 {a.get('path')}"))
    reg.register(Tool("test.run", ["test"], lambda a: "测试通过"))
    invoker = ToolInvoker(reg, audit_sink=audit.append)

    # 第①层:QA 无 fs.write 授权 → 拦截
    try:
        invoker.invoke("qa-acceptance-agent", "fs.write", {"path": "x.py"})
    except ToolPermissionDenied as e:
        print(f"   [①白名单] QA 调 fs.write 被拦: {e}")

    # 第③层:危险动作(路径穿越)→ 需确认
    try:
        invoker.invoke("backend-engineer-agent", "fs.write", {"path": "../../etc/x"})
    except ToolNeedsConfirmation as e:
        print(f"   [③危险检测] 命中: {e}")
        # 第④层:Host 带 confirmed=True 重放放行
        res = invoker.invoke(
            "backend-engineer-agent", "fs.write", {"path": "../../etc/x"}, confirmed=True
        )
        print(f"   [④Host 确认] 重放放行 → ok={res.ok} note={res.note}")

    print(f"   审计留痕 {len(audit)} 条: {[a['decision'] for a in audit]}")
    det = DangerDetector()
    hit = det.inspect(reg.get("fs.write"), {"cmd": "git reset --hard"})
    print(f"   DangerDetector 直测 'git reset --hard' → {hit}")


def demo_parallel(repo, registry) -> None:
    print("\n── ③ 无依赖节点并行(F-B.1):前后端并行 + 确定性 join ──────")
    adapter = MockAdapter(responder=_role_responder({
        "backend-engineer-agent": "后端:下单接口",
        "frontend-engineer-agent": "前端:下单页面",
    }))
    runner = NodeRunner(adapter, registry, repo, CostGuard(repo), CheckpointStore(repo))
    state = CompanyState(task_id=f"par-{uuid.uuid4().hex[:6]}")
    jobs = [
        ParallelJob("backend-engineer-agent", "实现下单后端"),
        ParallelJob("frontend-engineer-agent", "实现下单前端"),
    ]
    results = ParallelExecutor(runner).run(state, jobs)
    print(f"   并行收集 {len(results)} 个产物(与 jobs 同序):")
    for r in results:
        print(f"     - {r.role}: {r.artifact.summary}")
    print(f"   join 后 state.transition={state.transition}")
    print(f"   汇合后 artifacts 数={len(state.artifacts)} token={state.task_tokens}")


def demo_memory(repo) -> None:
    print("\n── ④ 三层记忆 + 三级 Compact(F-C.2/C.3):大产出落库留指针 ──")
    mem = MemoryManager(repo, namespace="runtime")
    task_id = f"mem-{uuid.uuid4().hex[:6]}"
    small = mem.record(task_id, "decision", "选用 FastAPI")
    big = mem.record(task_id, "artifact", "X" * 5000)  # 超 preview 预算
    print(f"   小产出原样留存: ref={small.ref}  text={small.text!r}")
    print(f"   大产出落库留指针: ref={big.ref}")
    print(f"   预览(prompt 内): {big.text[:48]!r}…")
    full = mem.fetch(big.ref)
    print(f"   按指针 demand-paging 取回完整内容长度={len(full) if full else 0}")

    # 命名空间隔离 + 检索回退(F-C.4/C.5)
    mem.remember("lesson", "下单接口需做幂等校验")
    edit_mem = MemoryManager(repo, namespace="edit")
    runtime_hit = Retriever(mem).retrieve("backend-engineer-agent", "下单 幂等")
    edit_hit = Retriever(edit_mem).retrieve("backend-engineer-agent", "下单 幂等")
    print(f"   runtime 命名空间检索命中 {len(runtime_hit.items)} 条;"
          f"edit 命名空间 {len(edit_hit.items)} 条 ← 命名空间隔离")


def demo_prefix_cache(repo, registry) -> None:
    print("\n── 前缀缓存(F-C.6):同一稳定前缀重复出现记 hit ──────────")
    cache = PrefixCache()
    adapter = MockAdapter()
    runner = NodeRunner(
        adapter, registry, repo, CostGuard(repo), CheckpointStore(repo),
        prefix_cache=cache,
    )
    state = CompanyState(task_id=f"pc-{uuid.uuid4().hex[:6]}")
    for _ in range(3):  # 同角色三次调用,稳定前缀一致 → 后两次命中
        runner.run(state, "backend-engineer-agent", "做点不同的事")
    print(f"   命中率={cache.hit_rate:.0%} (hits={cache.hits} misses={cache.misses})")


def main() -> None:
    print("[M3] Harness 加固能力离线 demo(MockAdapter,无需密钥)")
    repo = SqliteRepo(":memory:")
    registry = RoleRegistry()
    print(f"[M3] 已注册角色: {registry.list_ids()}")

    demo_node_retry(repo, registry)
    demo_tool_authz()
    demo_parallel(repo, registry)
    demo_memory(repo)
    demo_prefix_cache(repo, registry)

    repo.close()
    print("\n[M3] Harness 加固能力 demo 完成 ✓")


if __name__ == "__main__":
    main()
