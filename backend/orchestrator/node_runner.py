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
from backend.core.model_adapter.base import ModelAdapter
from backend.core.roles.prompt_composer import PromptComposer
from backend.core.roles.registry import RoleRegistry
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
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._repo = repo
        self._cost = cost_guard
        self._ckpt = checkpoints

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
        messages = PromptComposer.compose(role, task_text, upstream)

        # 成本熔断可能在记账时抛出 → 先记 role_start
        self._log(state, "role_start", role_id, tokens=0, latency_ms=0)

        # 当前档位放可变容器,软限触发后降档(影响后续自修复调用)
        tier = {"value": role.model_tier}

        def _charge(res, evt: str) -> None:
            check = self._cost.charge(state, res.tokens_total)
            if check.should_downgrade:
                tier["value"] = "small"  # 软限:后续调用降档小模型
                self._log(state, "cost_soft_limit", role_id, tokens=res.tokens_total,
                          latency_ms=res.latency_ms, note=f"task_tokens={check.task_tokens}")
            else:
                self._log(state, evt, role_id, tokens=res.tokens_total,
                          latency_ms=res.latency_ms)

        first = self._adapter.invoke(messages, schema=role.output_schema, tier=tier["value"])
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

        artifact, _results = parse_with_self_repair(first, reinvoke, state.retry_counters)

        # 回写 Artifact + 落 Checkpoint
        artifact.task_id = state.task_id
        artifact.role = role_id
        state.artifacts.append(artifact)
        state.transition = f"{role_id} -> {artifact.status.value}"
        self._ckpt.save(state)
        self._log(state, "artifact", role_id, tokens=0, latency_ms=0,
                  note=artifact.status.value)
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
