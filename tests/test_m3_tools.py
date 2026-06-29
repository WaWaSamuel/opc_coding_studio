"""M3 Tool 最小授权四层流(F-D.5 / M06)。

验证:
  - ① 白名单:QA 角色无 fs.write/code.build → ToolPermissionDenied
  - 授权角色正常执行,留痕
  - ③ 危险检测:危险动作未确认 → ToolNeedsConfirmation(警告但不阻断)
  - ④ Host 带 confirmed=True 重放 → 放行执行
  - DangerDetector 命中 rm -rf / git reset --hard / DROP TABLE / 路径穿越
  - ② Hook 可拦截
"""
from __future__ import annotations

import pytest

from backend.orchestrator.tools import (
    DangerDetector,
    Tool,
    ToolInvoker,
    ToolNeedsConfirmation,
    ToolPermissionDenied,
    ToolRegistry,
)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("fs.read", ["fs"], lambda a: "content"))
    reg.register(Tool("fs.write", ["fs"], lambda a: "written", dangerous=False))
    reg.register(Tool("code.build", ["build"], lambda a: "built"))
    reg.register(Tool("test.run", ["test"], lambda a: "passed"))
    reg.register(Tool("git.reset", ["git"], lambda a: "reset", dangerous=True))
    return reg


def test_whitelist_blocks_unauthorized_role():
    inv = ToolInvoker(_registry())
    # QA 无 fs.write 授权
    with pytest.raises(ToolPermissionDenied):
        inv.invoke("qa-acceptance-agent", "fs.write", {"path": "a.py"})
    # CEO 路由角色不碰任何 Tool
    with pytest.raises(ToolPermissionDenied):
        inv.invoke("ceo-orchestrator-agent", "fs.read", {})


def test_authorized_role_executes_and_audits():
    trail = []
    inv = ToolInvoker(_registry(), audit_sink=trail.append)
    res = inv.invoke("backend-engineer-agent", "fs.write", {"path": "app.py"})
    assert res.ok and res.output == "written"
    assert any(t["decision"] == "executed" for t in trail)


def test_dangerous_tool_needs_confirmation_then_passes():
    trail = []
    inv = ToolInvoker(_registry(), whitelist={"ops": {"git.reset"}},
                      audit_sink=trail.append)
    # 危险类工具未确认 → 警告但不阻断(抛需确认)
    with pytest.raises(ToolNeedsConfirmation):
        inv.invoke("ops", "git.reset", {"mode": "--hard"})
    assert any(t["decision"] == "needs_confirm" for t in trail)
    # Host 确认后重放 → 放行
    res = inv.invoke("ops", "git.reset", {"mode": "--hard"}, confirmed=True)
    assert res.ok and res.note == "confirmed"


def test_danger_detector_patterns():
    det = DangerDetector()
    safe = Tool("fs.write", ["fs"], lambda a: None)
    assert det.inspect(safe, {"cmd": "echo hello"}) is None
    assert det.inspect(safe, {"cmd": "rm -rf /tmp/x"}) is not None
    assert det.inspect(safe, {"cmd": "git reset --hard HEAD~1"}) is not None
    assert det.inspect(safe, {"sql": "DROP TABLE users"}) is not None
    assert det.inspect(safe, {"path": "../../etc/passwd"}) is not None


def test_hook_can_block():
    def blocking_hook(role, tool, args):
        if args.get("path", "").startswith("/etc"):
            raise PermissionError("禁止写系统目录")

    inv = ToolInvoker(_registry(), hooks=[blocking_hook])
    with pytest.raises(PermissionError):
        inv.invoke("backend-engineer-agent", "fs.write", {"path": "/etc/hosts"})
    # 正常路径放行
    res = inv.invoke("backend-engineer-agent", "fs.write", {"path": "src/a.py"})
    assert res.ok
