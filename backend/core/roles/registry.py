"""RoleRegistry:从 YAML 加载角色定义并内存索引(PRD M03)。

角色定义后续纳入 git 真源(M5);M1 先从本地 specs/ 目录加载。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from backend.schema import RoleSpec

_SPECS_DIR = Path(__file__).parent / "specs"


class RoleRegistry:
    def __init__(self, specs_dir: Path | None = None) -> None:
        self._dir = specs_dir or _SPECS_DIR
        self._roles: dict[str, RoleSpec] = {}
        self._load()

    def _load(self) -> None:
        if not self._dir.exists():
            return
        for path in sorted(self._dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            spec = RoleSpec(**data)
            self._roles[spec.role_id] = spec

    def get(self, role_id: str) -> RoleSpec:
        if role_id not in self._roles:
            raise KeyError(f"未注册的角色: {role_id}")
        return self._roles[role_id]

    def list_ids(self) -> list[str]:
        return list(self._roles.keys())

    def detail(self, role_id: str) -> dict:
        """角色完整元数据(供 GET /role/{id} / RoleInspector,M6 / F-A.8)。

        汇总 role_id / model_tier / 职责(role_prompt)/ 输出 schema 字段 /
        可调 Tool(来自 ROLE_TOOL_WHITELIST)。三层提示词的 common_prompt 是跨角色
        共用模板,不在详情里重复展示;role_prompt 即该角色的"职责说明"。
        """
        spec = self.get(role_id)
        # 延迟导入避免与 orchestrator 形成模块级循环依赖。
        from backend.orchestrator.tools import ROLE_TOOL_WHITELIST

        tools = sorted(ROLE_TOOL_WHITELIST.get(role_id, set()))
        schema_props = list(
            (spec.output_schema.get("properties", {}) or {}).keys()
        )
        return {
            "role_id": spec.role_id,
            "model_tier": spec.model_tier,
            "responsibility": spec.role_prompt.strip(),
            "output_schema_keys": schema_props,
            "tools": tools,
            "skills": [],  # 本期无独立 Skill 注册表;Tool 即角色可调能力。
        }
