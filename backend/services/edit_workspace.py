"""EditWorkspace:Edit 自改代码的"仓库接地"层(M6 / F-E.7 加固)。

为什么需要这一层(修复 Edit 改不出可运行代码的两个根因):
  1. **接地缺失**:Edit 部长/工程师此前靠模型脑补文件名,会定位到不存在的文件
     (如 web_style_config.yaml),改了也白改。这里提供 list_repo_files / read_targets,
     把"仓库里真实存在哪些可改文件、目标文件的真实内容"喂给模型,逼其基于事实定位。
  2. **content_hint 当正文覆盖**:此前落盘把模型的"改动说明"整段覆盖写文件,会把
     合法代码改坏。这里改为 search/replace 精确锚点:读真实文件 → 把 find 片段
     替换为 replace 片段 → 回写。锚点不命中即视为失败留痕,绝不整文件覆盖。

写路径仍复用 backend.orchestrator.tools.check_write_path 的白/黑名单兜底
(data//.env/.git/ 不可改),本层只在其之上做"读真实文件 + 精确替换"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.orchestrator.tools import (
    WRITE_ALLOW_PREFIXES,
    check_write_path,
)

# 列仓库文件时跳过的目录(噪声/产物/体积大,不该进模型上下文)。
_LIST_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", ".venv", "dist", "__pycache__", "data",
    "logs", ".run", ".pytest_cache", ".mypy_cache",
})
# 列仓库文件时只收这些后缀(可被 Edit 改的源码/配置/样式/文档)。
_LIST_EXTS: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".yaml", ".yml",
    ".json", ".md", ".sh", ".txt",
})


@dataclass
class SearchReplaceResult:
    """apply_search_replace 的结果(供工程师步落盘留痕)。"""

    applied: dict[str, int] = field(default_factory=dict)   # path -> 替换处数
    failed: list[dict[str, str]] = field(default_factory=list)  # 锚点未命中/读失败
    skipped: dict[str, str] = field(default_factory=dict)   # path -> 拒写原因

    @property
    def changed_files(self) -> list[str]:
        return sorted(self.applied.keys())

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": dict(self.applied),
            "failed": list(self.failed),
            "skipped": dict(self.skipped),
            "changed_files": self.changed_files,
        }


class EditWorkspace:
    """仓库接地 + search/replace 精确改写(以仓库根为基)。"""

    def __init__(self, repo_root: str | Path | None = None) -> None:
        self._root = Path(repo_root or Path.cwd()).resolve()

    # ── 接地①:列仓库真实可改文件(供部长定位)──────────────────
    def list_repo_files(self, limit: int = 400) -> list[str]:
        """返回白名单目录下真实存在的可改文件相对路径(POSIX,排序、截断)。"""
        out: list[str] = []
        for prefix in WRITE_ALLOW_PREFIXES:
            base = self._root / prefix
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                if any(part in _LIST_SKIP_DIRS for part in p.relative_to(self._root).parts):
                    continue
                if p.suffix not in _LIST_EXTS:
                    continue
                rel = p.relative_to(self._root).as_posix()
                # 复用写路径裁决:只列"将来确实可写"的文件,定位与落盘口径一致。
                if check_write_path(rel) is None:
                    out.append(rel)
        out = sorted(set(out))
        return out[:limit]

    # ── 接地②:读目标文件真实内容(供工程师基于事实产 diff)──────
    def read_targets(self, paths: list[str], max_bytes: int = 8_000) -> dict[str, str]:
        """读取给定相对路径的真实内容(截断 max_bytes);不存在/越界则跳过。"""
        result: dict[str, str] = {}
        for raw in paths or []:
            rel = self._safe_rel(raw)
            if rel is None:
                continue
            p = self._root / rel
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            result[rel] = text[:max_bytes]
        return result

    # ── 落盘:search/replace 精确改写(取代 content_hint 整文件覆盖)──
    def apply_search_replace(
        self, changes: list[dict[str, Any]]
    ) -> SearchReplaceResult:
        """对每项 {path, find, replace} 做精确替换并回写真实文件。

        规则(稳妥优先,对齐 aider/claude-code 的 search/replace 思路):
          - path 必须过 check_write_path 白名单,否则计 skipped。
          - find 为空 → 视为新建/追加(replace 即全文);文件不存在则创建。
          - find 非空但在真实内容里找不到 → 计 failed(绝不盲改),不落盘。
          - 命中则把首处(或全部)出现替换为 replace,回写文件。
        """
        res = SearchReplaceResult()
        # 同一文件多处改动累积在内存后一次性回写,避免 find 锚点被前一处替换破坏。
        pending: dict[str, str] = {}
        for ch in changes or []:
            path = (ch.get("path") or "").strip()
            if not path:
                res.failed.append({"path": "", "reason": "缺少 path"})
                continue
            reason = check_write_path(path)
            if reason is not None:
                res.skipped[path] = reason
                continue
            find = ch.get("find", "") or ""
            replace = ch.get("replace", "") or ""
            p = self._root / path

            if path in pending:
                current = pending[path]
            elif p.is_file():
                try:
                    current = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    res.failed.append({"path": path, "reason": f"读失败: {exc}"})
                    continue
            else:
                current = None  # 文件不存在

            if not find:
                # 新建/全量写:文件不存在 → 用 replace 建;存在 → 跳过(避免误清空)。
                if current is None:
                    pending[path] = replace
                    res.applied[path] = res.applied.get(path, 0) + 1
                else:
                    res.failed.append(
                        {"path": path, "reason": "find 为空但文件已存在,拒绝整文件覆盖"}
                    )
                continue

            if current is None:
                res.failed.append({"path": path, "reason": "目标文件不存在,无法定位 find 锚点"})
                continue
            if find not in current:
                res.failed.append({"path": path, "reason": "find 锚点未在文件中命中"})
                continue
            count = current.count(find)
            pending[path] = current.replace(find, replace)
            res.applied[path] = res.applied.get(path, 0) + count

        # 一次性回写命中的文件
        for path, content in pending.items():
            p = self._root / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return res

    # ── 内部:相对路径归一(防越界)──────────────────────────────
    def _safe_rel(self, raw: str) -> str | None:
        s = (raw or "").strip().replace("\\", "/")
        if not s or s.startswith("/") or s.startswith("~"):
            return None
        if ".." in Path(s).parts:
            return None
        return s
