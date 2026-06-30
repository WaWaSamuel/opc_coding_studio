"""ServiceRestarter:服务自重启(M6 / F-E.6)。

Edit 改了 backend/** 或 frontend/** 且经回归 + Host 确认 Merge 后,改动只有
重启对应进程才生效。把"重启"收口到一处,封装为可被 POST /edit/restart 调用:

  - 闸门 edit_auto_restart_enabled(默认 False):关 → 只回 restart_required
    信号(dry-run),由 Host 手动 `./scripts/opc.sh restart`,绝不擅自重启;
    开 → 才真正脱离当前请求进程重启前/后端。
  - 脱离进程:重启后端会杀掉当前 uvicorn 进程,故必须用 start_new_session 起
    一个独立子进程(opc.sh restart),它先等当前请求返回再换进程,避免自杀式中断。
  - health 兜底:重启后轮询 GET /health;失败 → git revert 回滚,保命。

Token / 密钥绝不出现在返回或日志中。
"""
from __future__ import annotations

import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Scope = Literal["backend", "frontend", "both"]

_ROOT = Path(__file__).resolve().parents[2]
_OPC = _ROOT / "scripts" / "opc.sh"


@dataclass
class RestartResult:
    ok: bool
    scope: str
    dry_run: bool
    health: dict[str, Any]
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "scope": self.scope, "dry_run": self.dry_run,
            "health": self.health, "note": self.note,
        }


class ServiceRestarter:
    """脱离当前请求进程重启前/后端(受 edit_auto_restart_enabled 闸门控制)。"""

    def __init__(
        self,
        enabled: bool | None = None,
        backend_port: int | None = None,
        opc_script: Path | None = None,
        git_service: Any = None,
    ) -> None:
        from backend.config import settings

        self._enabled = (
            settings.edit_auto_restart_enabled if enabled is None else enabled
        )
        self._port = backend_port or settings.api_port
        self._opc = opc_script or _OPC
        self._git = git_service  # 失败回滚兜底(可选注入)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def restart(self, scope: Scope = "both", *, delay: float = 1.0) -> RestartResult:
        """重启指定范围;dry-run 时只回 restart_required 不动进程。

        delay:子进程在真正换进程前的等待秒数,留出时间让当前 HTTP 响应先返回
        (避免重启后端把"正在回包的自己"杀掉)。
        """
        if scope not in ("backend", "frontend", "both"):
            return RestartResult(False, scope, not self._enabled, {},
                                 note=f"未知 scope: {scope}")
        if not self._enabled:
            # 保命默认:不擅自重启,回信号让 Host 手动重启。
            return RestartResult(
                True, scope, dry_run=True, health={},
                note="restart_required:自重启闸门关闭,请手动 ./scripts/opc.sh restart",
            )
        if not self._opc.exists() or shutil.which("bash") is None:
            return RestartResult(False, scope, False, {},
                                 note=f"找不到运维脚本或 bash:{self._opc}")

        self._spawn_detached(scope, delay)
        # 后端被换进程后,当前进程即将退出;health 轮询主要对 frontend-only 有意义。
        health: dict[str, Any] = {}
        if scope == "frontend":
            health = self._wait_health()
            ok = bool(health.get("ok"))
            if not ok and self._git is not None:
                self._git.revert("HEAD")
                return RestartResult(False, scope, False, health,
                                     note="health 失败,已 git revert 回滚")
            return RestartResult(ok, scope, False, health,
                                 note="frontend 重启完成" if ok else "health 未就绪")
        return RestartResult(True, scope, False, health,
                             note=f"已派发 {scope} 重启(脱离当前进程)")

    # --- 内部 ---
    def _opc_args(self, scope: Scope) -> list[str]:
        # 重启后端会换掉当前进程;只重前端用 --backend-only 之外的语义不便表达,
        # 故 frontend-only 走 stop 前端 + run(run 默认带前端,幂等)。
        if scope == "frontend":
            return ["restart"]
        if scope == "backend":
            return ["restart", "--backend-only"]
        return ["restart"]

    def _spawn_detached(self, scope: Scope, delay: float) -> None:
        # 先 sleep 让当前请求把响应回完,再执行 opc.sh restart;start_new_session
        # 让子进程脱离当前进程组,后端被 kill 时不会连带杀掉这个重启器。
        opc_cmd = " ".join(["bash", str(self._opc), *self._opc_args(scope)])
        shell = f"sleep {max(delay, 0):.1f}; {opc_cmd}"
        subprocess.Popen(  # noqa: S603 — 固定脚本路径,scope 受白名单约束
            ["bash", "-lc", shell],
            cwd=str(_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _wait_health(self, timeout: float = 30.0,
                     interval: float = 1.0) -> dict[str, Any]:
        url = f"http://127.0.0.1:{self._port}/health"
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
                    if resp.status == 200:
                        return {"ok": True, "status": "ok"}
                    last = f"http {resp.status}"
            except Exception as exc:  # noqa: BLE001 — 重启窗口内连不上属正常
                last = type(exc).__name__
            time.sleep(interval)
        return {"ok": False, "error": last or "timeout"}
