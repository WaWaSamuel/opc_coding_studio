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
