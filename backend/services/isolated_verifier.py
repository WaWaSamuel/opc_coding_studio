"""IsolatedVerifier:隔离实例真实验证(M7 / F-E.8)。

为什么需要这一层(在"回归套件通过"之上再加一道"改完真能跑"的硬证据):
  回归套件跑的是当前进程里的逻辑用例,无法证明"Edit 改完的代码作为一个全新的
  全栈实例能否真正被拉起并对外服务"。本层在 Edit 提 PR 前,把 feature 分支的改动
  检出到一个**隔离的 git worktree**(独立工作树,不污染主仓库/主进程),用**独立端口
  + 独立 sqlite 库**起一个临时后端实例,真实探活 GET /health;通过才放行评审提 PR,
  失败即判 reject 回退工程师。验证完无论成败都清理 worktree 与子进程,零残留。

保命默认(edit_isolated_verify_enabled,默认 False):
  - 关:verify() 直接返回 skipped=True(视为通过,沿用回归套件 + Host 确认即可),
    不起任何子进程,保证离线/测试确定性、省资源。
  - 开:才真正建 worktree + 起独立 uvicorn 探活。仅探后端 /health(前端构建慢且
    dev 由 Vite HMR 生效,这里聚焦"后端改动是否还能起服务")。

边界与安全:
  - 仅在 edit_git_enabled 为真(改动已真实落到 feature 分支可被 worktree 检出)时才
    有意义;dry-run 下没有真实分支可检出,verify() 也回 skipped。
  - 子进程用 start_new_session 脱离,超时/结束统一 kill 进程组,worktree 用
    `git worktree remove --force` 清理;临时库随 worktree 目录一并删除。
  - 绝不触碰主实例的端口与 DB。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class VerifyResult:
    ok: bool
    skipped: bool = False
    health: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    logs: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "skipped": self.skipped, "health": self.health,
            "note": self.note, "logs": self.logs[-1000:],
        }


class IsolatedVerifier:
    """git worktree + 独立端口/库 起临时全栈实例真实探活(F-E.8)。"""

    def __init__(
        self,
        git_service: Any,
        enabled: bool | None = None,
        port: int | None = None,
        repo_root: Path | None = None,
    ) -> None:
        from backend.config import settings

        self._git = git_service
        self._enabled = (
            settings.edit_isolated_verify_enabled if enabled is None else enabled
        )
        self._port = port or settings.edit_isolated_verify_port
        self._root = repo_root or _ROOT

    @property
    def enabled(self) -> bool:
        return self._enabled

    def verify(self, branch: str, *, boot_timeout: float = 40.0) -> VerifyResult:
        """在隔离 worktree 上以独立端口/库起后端实例并探活。

        返回 ok=True 表示该 feature 分支的后端改动能被真实拉起;skipped=True 表示
        闸门关或无真实分支可检出(视为通过,不阻断主链路)。
        """
        # 闸门关 / 非真实 git(无分支可检出)→ 跳过,视为通过(不阻断)。
        if not self._enabled:
            return VerifyResult(True, skipped=True,
                                note="隔离实例验证闸门关闭,跳过(沿用回归套件)")
        if not getattr(self._git, "enabled", False):
            return VerifyResult(True, skipped=True,
                                note="dry-run 无真实分支可检出,跳过隔离验证")
        if shutil.which("git") is None:
            return VerifyResult(True, skipped=True, note="环境无 git,跳过")

        work_dir = Path(tempfile.mkdtemp(prefix="opc_verify_"))
        wt = work_dir / "repo"
        proc: subprocess.Popen | None = None
        try:
            # 1) 检出 feature 分支到隔离 worktree(不切换主仓库当前分支)。
            try:
                self._git_cmd("worktree", "add", "--force", str(wt), branch)
            except Exception as exc:  # noqa: BLE001
                return VerifyResult(False, note=f"git worktree 检出失败: {exc}")

            # 2) 独立 sqlite 库(临时目录内),独立端口,起后端探活。
            env = dict(os.environ)
            env["DB_PATH"] = str(work_dir / "verify.db")
            env["API_PORT"] = str(self._port)
            env["API_HOST"] = "127.0.0.1"
            env["OPC_ENABLE_LARK"] = "0"          # 隔离实例不连飞书
            env["SCHEDULER_ENABLED"] = "0"        # 不起调度器
            env["MODEL_PROVIDER"] = "mock"        # 验证"能否起服务",不烧真实模型
            python = self._python_bin()
            proc = subprocess.Popen(  # noqa: S603 — 固定模块入口,端口受配置约束
                [python, "-m", "backend.main"],
                cwd=str(wt), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )

            # 3) 轮询 /health。
            health = self._wait_health(self._port, timeout=boot_timeout)
            logs = self._drain_logs(proc)
            if health.get("ok"):
                return VerifyResult(True, health=health,
                                    note=f"隔离实例(:{self._port})探活通过", logs=logs)
            return VerifyResult(False, health=health,
                                note="隔离实例未在超时内就绪(改动可能导致起服务失败)",
                                logs=logs)
        finally:
            self._terminate(proc)
            # 清理 worktree 登记 + 临时目录(无论成败,零残留)。
            try:
                self._git_cmd("worktree", "remove", "--force", str(wt), check=False)
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(work_dir, ignore_errors=True)

    # ── 内部 ──────────────────────────────────────────────────
    def _python_bin(self) -> str:
        venv = self._root / ".venv" / "bin" / "python"
        return str(venv) if venv.exists() else "python3"

    def _git_cmd(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=str(self._root),
            capture_output=True, text=True,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "git failed")
        return proc.stdout

    def _wait_health(self, port: int, timeout: float,
                     interval: float = 1.0) -> dict[str, Any]:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
                    if resp.status == 200:
                        return {"ok": True, "status": "ok", "port": port}
                    last = f"http {resp.status}"
            except Exception as exc:  # noqa: BLE001 — 启动窗口内连不上属正常
                last = type(exc).__name__
            time.sleep(interval)
        return {"ok": False, "error": last or "timeout", "port": port}

    @staticmethod
    def _drain_logs(proc: subprocess.Popen | None) -> str:
        if proc is None or proc.stdout is None:
            return ""
        try:
            # 非阻塞地把已有输出读出来(进程仍在跑,这里只取片段供诊断)。
            import select

            chunks: list[str] = []
            while True:
                r, _, _ = select.select([proc.stdout], [], [], 0)
                if not r:
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                chunks.append(line)
            return "".join(chunks)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _terminate(proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=5.0)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
