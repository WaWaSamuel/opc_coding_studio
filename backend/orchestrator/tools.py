"""Tool / Skill 执行层(M06 / F-D.5)。

统一工具注册 + 最小授权调用。四层授权流(借 Claude Code,补 G5):
  ① 配置规则:角色↔Tool 白名单(最快,先拦没授权的角色)
  ② Hook:可扩展校验钩子(预留扩展点,默认放行)
  ③ 规则/AST 危险动作检测:DangerDetector 识别 fs 越界写 / git reset --hard /
     删表 等;危险动作"**警告但不阻断,需显式确认**"
  ④ Host 二次确认:危险动作兜底,未经确认拒绝执行

设计取舍(M3 范围):
  - 不真正执行 shell/fs(那是 M4+ 沙箱的事),本层先把"授权决策 + 留痕"做对,
    handler 由调用方注入(测试/真实皆可),保证授权管线本身可独立验证。
  - 危险检测用规则匹配(PRD 提到 tree-sitter AST 是 Claude Code 的做法,
    本期 Python 栈先用规则,AST 留后续);"警告但不阻断"= 返回 needs_confirm,
    由 Host 决定是否带 confirmed=True 重放。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.errors import OpcError


class ToolPermissionDenied(OpcError):
    """角色无该 Tool 授权(第①层拦截)。"""


class ToolNeedsConfirmation(OpcError):
    """危险动作需 Host 二次确认(第③/④层)。"""

    def __init__(self, tool: str, reason: str):
        self.tool = tool
        self.reason = reason
        super().__init__(f"危险动作需确认 [{tool}]: {reason}")


@dataclass
class Tool:
    name: str                       # 如 fs.write / code.build / git.commit
    scopes: list[str]               # 该工具需要的能力域(用于授权语义)
    handler: Callable[[dict[str, Any]], Any]
    dangerous: bool = False         # 是否属危险类(写/删/不可逆)
    schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    tool: str
    ok: bool
    output: Any = None
    note: str = ""


# 角色 → 允许调用的 Tool 白名单(第①层)。
# PRD 边界:QA 角色无 fs.write/code.build。
ROLE_TOOL_WHITELIST: dict[str, set[str]] = {
    "backend-engineer-agent": {"fs.read", "fs.write", "code.build", "test.run"},
    "frontend-engineer-agent": {"fs.read", "fs.write", "code.build", "test.run"},
    "qa-acceptance-agent": {"fs.read", "test.run"},          # 无 fs.write/code.build
    "loop-judge-agent": {"fs.read", "test.run"},
    "dev-lead-agent": {"fs.read"},
    "ceo-orchestrator-agent": set(),                         # 路由角色不碰 Tool
    # M5 Edit 角色(改系统自身,纳管 git):
    #   工程师产改动(feature 分支 diff)、评审提 PR;回归官只读 + 跑测;
    #   部长只定位不直接动 git。push/PR 仍受 GitService 闸门 + Host 确认兜底。
    "edit-lead-agent": {"fs.read", "git.diff"},
    "edit-engineer-agent": {"fs.read", "fs.write", "git.branch", "git.diff",
                            "git.commit"},
    "edit-regression-agent": {"fs.read", "test.run", "git.diff"},
    "edit-review-agent": {"fs.read", "git.diff", "git.pr", "git.revert"},
}


class DangerDetector:
    """规则/AST 危险动作检测(第③层)。本期用规则,AST 留后续。"""

    # 危险命令模式:不可逆/越权写/删
    _DANGER_PATTERNS = [
        (re.compile(r"\brm\s+-rf\b"), "递归强删 (rm -rf)"),
        (re.compile(r"git\s+reset\s+--hard"), "git reset --hard 丢弃改动"),
        (re.compile(r"git\s+push\s+.*--force"), "git push --force 覆盖远端"),
        (re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE), "删表 (DROP TABLE)"),
        (re.compile(r"\bTRUNCATE\b", re.IGNORECASE), "清空表 (TRUNCATE)"),
        (re.compile(r"\.\./"), "路径穿越 (../) 可能越界写"),
    ]

    def inspect(self, tool: Tool, args: dict[str, Any]) -> str | None:
        """返回危险原因(命中则需确认);None 表示安全。"""
        if tool.dangerous:
            return f"工具 {tool.name} 标记为危险类(写/删/不可逆)"
        blob = " ".join(str(v) for v in args.values())
        for pat, reason in self._DANGER_PATTERNS:
            if pat.search(blob):
                return reason
        return None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"未注册的 Tool: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools)


Hook = Callable[[str, Tool, dict[str, Any]], None]
AuditSink = Callable[[dict[str, Any]], None]


class ToolInvoker:
    """四层授权流的执行入口,鉴权 + 执行 + 留痕。"""

    def __init__(
        self,
        registry: ToolRegistry,
        whitelist: dict[str, set[str]] | None = None,
        detector: DangerDetector | None = None,
        hooks: list[Hook] | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._registry = registry
        self._whitelist = whitelist if whitelist is not None else ROLE_TOOL_WHITELIST
        self._detector = detector or DangerDetector()
        self._hooks = hooks or []
        self._audit = audit_sink

    def invoke(
        self,
        role_id: str,
        tool_name: str,
        args: dict[str, Any],
        confirmed: bool = False,
    ) -> ToolResult:
        tool = self._registry.get(tool_name)

        # ① 配置规则:角色↔Tool 白名单
        allowed = self._whitelist.get(role_id, set())
        if tool_name not in allowed:
            self._trail(role_id, tool_name, "denied", "角色无授权")
            raise ToolPermissionDenied(
                f"角色 {role_id} 无权调用 {tool_name}(白名单: {sorted(allowed)})"
            )

        # ② Hook:可扩展校验(默认放行;hook 内可自行抛异常拦截)
        for hook in self._hooks:
            hook(role_id, tool, args)

        # ③ 危险动作检测 → ④ Host 二次确认
        danger = self._detector.inspect(tool, args)
        if danger and not confirmed:
            # 警告但不阻断:返回需确认,由 Host 带 confirmed=True 重放
            self._trail(role_id, tool_name, "needs_confirm", danger)
            raise ToolNeedsConfirmation(tool_name, danger)

        # 通过四层 → 执行 + 留痕
        output = tool.handler(args)
        note = "confirmed" if (danger and confirmed) else "ok"
        self._trail(role_id, tool_name, "executed", note)
        return ToolResult(tool=tool_name, ok=True, output=output, note=note)

    def _trail(self, role: str, tool: str, decision: str, reason: str) -> None:
        if self._audit is not None:
            self._audit({
                "role": role, "tool": tool,
                "decision": decision, "reason": reason,
            })


def register_git_tools(registry: "ToolRegistry", git_service: Any) -> "ToolRegistry":
    """把 git.* Tool 注册进 ToolRegistry(M09 / F-E.4)。

    Tool handler 委托给 GitService;授权/危险检测/Host 确认仍由 ToolInvoker
    四层流统一裁决。git.commit / git.revert 标记 dangerous=True(写/不可逆),
    命中第③/④层需 Host 确认;git.branch / git.diff / git.pr 为非破坏性。
    """
    registry.register(Tool(
        name="git.branch", scopes=["vcs"],
        handler=lambda a: git_service.checkout_new_branch(a["branch"]),
    ))
    registry.register(Tool(
        name="git.diff", scopes=["vcs"],
        handler=lambda a: git_service.diff(a.get("base"), a.get("head")),
    ))
    registry.register(Tool(
        name="git.commit", scopes=["vcs"], dangerous=True,
        handler=lambda a: git_service.commit(a["message"], a.get("files")),
    ))
    registry.register(Tool(
        name="git.revert", scopes=["vcs"], dangerous=True,
        handler=lambda a: git_service.revert(a.get("commit", "HEAD")),
    ))
    registry.register(Tool(
        name="git.pr", scopes=["vcs"],
        handler=lambda a: git_service.open_pr(
            a["branch"], a.get("summary", ""), a.get("badcase_ref", "")
        ),
    ))
    return registry
