"""NodeRunner:包裹一次无状态角色调用(PRD M02 / F-B.10)。

一步闭环:
  组装三层上下文 → ModelAdapter.invoke → 成本记账+熔断(F-D.6)
  → JSON 解析/自修复(F-D.3) → 回写 Artifact 到 State → 落 Checkpoint(F-D.4)
  → 发流转日志/成本记账(F-A.6,M1 落库,事件总线在 M4)。

无状态隔离:每次只用本角色三层 + 上一棒 Artifact,不含他角色提示词。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from backend.core.cost_guard import CostGuard
from backend.core.event_bus import EventBus
from backend.core.memory import MemoryManager
from backend.core.model_adapter.base import ModelAdapter
from backend.core.retrieval import PrefixCache, Retriever
from backend.core.roles.prompt_composer import PromptComposer
from backend.core.roles.registry import RoleRegistry
from backend.orchestrator.retry import RetryController
from backend.orchestrator.self_repair import parse_with_self_repair
from backend.repo.checkpoint_store import CheckpointStore
from backend.repo.repository import Repository
from backend.schema import Artifact, CompanyState


class NodeRunner:
    def __init__(
        self,
        adapter: ModelAdapter,
        registry: RoleRegistry,
        repo: Repository,
        cost_guard: CostGuard,
        checkpoints: CheckpointStore,
        retry: RetryController | None = None,
        memory: MemoryManager | None = None,
        prefix_cache: PrefixCache | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._repo = repo
        self._cost = cost_guard
        self._ckpt = checkpoints
        self._retry = retry or RetryController()
        # M3 harness:检索注入 + 前缀缓存统计(都可选,缺省不改变 M1/M2 行为)
        self._memory = memory
        self._retriever = Retriever(memory) if memory is not None else None
        self._prefix_cache = prefix_cache
        # M4:事件总线(可选)。节点级 token 仍以 repo._log 为成本真源;
        # EventBus 这里只做实时流式推送(persist=False),避免重复落库双计。
        self._bus = event_bus

    def run(
        self,
        state: CompanyState,
        role_id: str,
        task_text: str,
        upstream: Artifact | None = None,
    ) -> Artifact:
        role = self._registry.get(role_id)
        state.current_role = role_id
        if upstream is None:
            upstream = state.artifacts[-1] if state.artifacts else None

        # M4:角色起手事件(实时流式;不落库,审计真源仍是 _log 的 tool_call/artifact)
        self._stream(state.task_id, "role_start", role_id,
                     {"task_text": task_text[:200]})

        # F-C.5 检索式记忆注入(置于 CACHE_BOUNDARY 前稳定区)
        memory_block = ""
        if self._retriever is not None:
            retrieved = self._retriever.retrieve(role_id, task_text)
            memory_block = retrieved.as_prompt_block()
            if retrieved.items:
                state.memory_refs = list(dict.fromkeys(
                    state.memory_refs + [it["text"][:40] for it in retrieved.items]
                ))

        messages = PromptComposer.compose(
            role, task_text, upstream, memory_block=memory_block
        )

        # F-C.6 稳定前缀缓存命中统计
        if self._prefix_cache is not None:
            self._prefix_cache.observe(messages)

        # 当前档位放可变容器,软限触发后降档(影响后续自修复调用)
        tier = {"value": role.model_tier}

        def _charge(res, evt: str) -> None:
            check = self._cost.charge(state, res.tokens_total)
            if check.should_downgrade:
                tier["value"] = "small"  # 软限:后续调用降档小模型
                self._log(state, "cost_soft_limit", role_id, tokens=res.tokens_total,
                          latency_ms=res.latency_ms, note=f"task_tokens={check.task_tokens}")
                self._stream(state.task_id, "cost_soft_limit", role_id,
                             {"task_tokens": check.task_tokens},
                             tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                             latency_ms=res.latency_ms)
            else:
                self._log(state, evt, role_id, tokens=res.tokens_total,
                          latency_ms=res.latency_ms)
                self._stream(state.task_id, "tool_call", role_id, {"kind": evt},
                             tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                             latency_ms=res.latency_ms)

        def _invoke_once() -> Artifact:
            """一次完整节点调用:模型 invoke + JSON 自修复。

            内层 self_repair 处理 JSON 不合法(回喂修正);若自修复仍耗尽
            或模型 API 5xx/限流,会抛 SelfRepairExhausted/ModelCallError,
            交外层 RetryController 做节点级原地重试(F-D.2,与业务回退独立)。
            """
            self._stream(state.task_id, "thinking", role_id,
                         {"note": "组装上下文并调用模型"})
            first = self._adapter.invoke(
                messages, schema=role.output_schema, tier=tier["value"]
            )
            _charge(first, "tool_call")

            def reinvoke(err_msg: str):
                repair_messages = messages + [
                    {"role": "assistant", "content": first.content},
                    {"role": "user", "content": err_msg},
                ]
                res = self._adapter.invoke(
                    repair_messages, schema=role.output_schema, tier=tier["value"]
                )
                _charge(res, "self_repair")
                return res

            art, _results = parse_with_self_repair(
                first, reinvoke, state.retry_counters
            )
            return art

        def _on_retry(n: int, exc: Exception) -> None:
            self._log(state, "node_retry", role_id, tokens=0, latency_ms=0,
                      note=f"attempt={n} err={type(exc).__name__}: {exc}")
            self._stream(state.task_id, "node_retry", role_id,
                         {"attempt": n, "error": f"{type(exc).__name__}: {exc}"})

        artifact = self._retry.run(
            state, f"node:{role_id}", _invoke_once, on_retry=_on_retry
        )

        # 回写 Artifact + 落 Checkpoint
        artifact.task_id = state.task_id
        artifact.role = role_id
        state.artifacts.append(artifact)
        state.transition = f"{role_id} -> {artifact.status.value}"
        self._ckpt.save(state)
        self._log(state, "artifact", role_id, tokens=0, latency_ms=0,
                  note=artifact.status.value)
        self._stream(state.task_id, "artifact", role_id, {
            "status": artifact.status.value,
            "files": artifact.artifact.files,
            "summary": artifact.artifact.summary[:200],
        })
        return artifact

    def checkpoint(self, state: CompanyState) -> None:
        """图级别落盘(节点流转之外的状态变更,如最终 status)。"""
        self._ckpt.save(state)

    def _log(self, state: CompanyState, event: str, role: str, *, tokens: int,
             latency_ms: int, note: str = "") -> None:
        self._repo.append_log(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "task_id": state.task_id,
                "system": state.system,
                "role": role,
                "event": event,
                "tokens": tokens,
                "latency_ms": latency_ms,
                "task_tokens": state.task_tokens,
                "note": note,
            }
        )

    def _stream(self, task_id: str, event: str, role: str,
                payload: dict, *, tokens_in: int = 0, tokens_out: int = 0,
                latency_ms: int = 0) -> None:
        """实时推流到 EventBus(不落库,审计真源是 _log)。无 bus 时静默跳过。"""
        if self._bus is None:
            return
        self._bus.emit(task_id, event, role=role, payload=payload,
                       tokens_in=tokens_in, tokens_out=tokens_out,
                       latency_ms=latency_ms, persist=False)
