"""三层提示词组装(F-C.1)。

组装顺序(对齐 PRD C 方案,缓存边界):
  公共层 → 角色层 → 长期记忆 Top-K ┊CACHE_BOUNDARY┊ 任务层 → 上一棒 Artifact
边界前稳定,利于 prompt caching(F-C.6;M1 仅插标记,M3 起实际命中统计)。

无状态隔离(F-B.10):每次只含本角色三层 + 检索记忆 + 上一棒 Artifact,
不含他角色提示词。
"""
from __future__ import annotations

import json

from backend.schema import Artifact, RoleSpec

CACHE_BOUNDARY = "┊CACHE_BOUNDARY┊"


class PromptComposer:
    @staticmethod
    def compose(
        role: RoleSpec,
        task_text: str,
        upstream: Artifact | None = None,
        memory_block: str = "",
    ) -> list[dict[str, str]]:
        # 稳定前缀:公共层 + 角色层 + 输出 schema 约定 + 长期记忆 Top-K
        system_parts = [role.common_prompt.strip(), role.role_prompt.strip()]
        if role.output_schema:
            system_parts.append(
                "你必须只输出一个合法 JSON 对象,字段严格符合以下 schema:\n"
                + json.dumps(role.output_schema, ensure_ascii=False)
            )
        if memory_block.strip():
            # 检索式记忆注入,置于边界前稳定区(F-C.5,随角色/任务变化有限)
            system_parts.append(memory_block.strip())
        system = "\n\n".join(p for p in system_parts if p)
        system += f"\n{CACHE_BOUNDARY}"

        # 动态后缀:任务层 + 上一棒 Artifact
        user_parts = [f"# 任务\n{task_text.strip()}"]
        if upstream is not None:
            user_parts.append(
                "# 上一棒交付物(Artifact)\n"
                + upstream.model_dump_json(indent=2)
            )
        user = "\n\n".join(user_parts)

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
