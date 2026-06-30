"""GitService:远端 GitHub 版本管理(M09 / F-E.4)。

把"改系统自身"的版本动作收口到一处,封装为可被 git.* Tool 调用的服务:
  - BranchManager:从 main 切 feature/* 分支
  - 改动落盘(apply_changes)+ 暂存提交(commit,message 记 badcase/优化项满足审计)
  - diff(feature ↔ main,供回归 + 可视化 diff 高亮)
  - revert(异常时 git revert 回滚)
  - PRComposer:产出 PR 标题/正文/pr_url

保命默认(对齐用户确认的"本地 git + 受控 PR,默认不自动推远端"):
  - enabled=False(EDIT_GIT_ENABLED=0):**dry-run**,不触碰真实仓库,只把
    计划动作记进 plan,供评审/可视化;保证离线确定性、不可逆动作零风险。
  - enabled=True:在本地工作树真实建分支/commit/diff/revert。
  - push_enabled=True 且 GITHUB_TOKEN 就绪:才允许真实 push + 建远端 PR;
    否则 open_pr 走 dry-run,产出本地 PR 描述与占位 pr_url(等 Host 确认)。

Token 仅经环境变量(F-F.4),绝不写入 commit/日志/diff。
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.errors import OpcError


class GitError(OpcError):
    """git 命令执行失败。"""


def _slug(text: str, limit: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit] or "change").strip("-")


@dataclass
class GitResult:
    ok: bool
    output: str = ""
    note: str = ""


@dataclass
class PRDraft:
    """PRComposer 产出的变更评审包(供 Host 确认 Merge)。"""

    branch: str
    title: str
    body: str
    pr_url: str
    pushed: bool = False
    dry_run: bool = True


class BranchManager:
    """feature/* 分支命名 + 创建(从 main 切出)。"""

    def __init__(self, git: "GitService") -> None:
        self._git = git

    def feature_name(self, hint: str) -> str:
        return f"feature/{_slug(hint)}-{int(time.time())}"

    def create(self, hint: str) -> str:
        branch = self.feature_name(hint)
        self._git.checkout_new_branch(branch)
        return branch


class PRComposer:
    """把 feature 分支改动组织成可审计的 PR 描述。

    commit message / PR body 记录"解决什么 badcase / 对应哪条优化项"满足审计。
    """

    @staticmethod
    def commit_message(summary: str, badcase_ref: str = "", todo_ref: str = "") -> str:
        lines = [f"edit: {summary}".strip()]
        if badcase_ref:
            lines.append("")
            lines.append(f"Badcase: {badcase_ref}")
        if todo_ref:
            lines.append(f"Optimization: {todo_ref}")
        return "\n".join(lines)

    @staticmethod
    def compose(branch: str, summary: str, diff_text: str,
                badcase_ref: str = "", pr_url: str = "",
                pushed: bool = False, dry_run: bool = True) -> PRDraft:
        title = f"[Edit] {summary}".strip()
        body_parts = [
            "## 变更概述", summary or "(无)",
            "", "## 关联 Badcase / 优化项",
            badcase_ref or "(无显式关联)",
            "", "## Diff 摘要",
            "```diff",
            diff_text.strip()[:4000] or "(空 diff)",
            "```",
            "", "## 闸门",
            "- 回归测试官:≥95% 通过(劣化即回退,不提 PR)",
            "- Host 确认 Merge 后 main 生效;异常 git revert 回滚",
        ]
        return PRDraft(
            branch=branch, title=title, body="\n".join(body_parts),
            pr_url=pr_url, pushed=pushed, dry_run=dry_run,
        )


class GitService:
    """封装 git.* Tool 的版本动作(本地优先,受控推远端)。"""

    def __init__(
        self,
        repo_dir: str | Path | None = None,
        enabled: bool | None = None,
        push_enabled: bool | None = None,
        token: str | None = None,
        main_branch: str | None = None,
        remote: str | None = None,
    ) -> None:
        from backend.config import settings

        self._dir = Path(repo_dir or Path.cwd())
        self._enabled = settings.edit_git_enabled if enabled is None else enabled
        self._push = settings.edit_push_enabled if push_enabled is None else push_enabled
        self._token = (token if token is not None else settings.github_token) or ""
        self._main = main_branch or settings.git_main_branch
        self._remote = remote or settings.github_repo
        self.branches = BranchManager(self)
        # dry-run 计划留痕(供评审/可视化;真实改动时也记录已执行动作)
        self.plan: list[dict[str, Any]] = []

    # --- 能力位 ---
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def can_push(self) -> bool:
        return bool(self._enabled and self._push and self._token)

    @property
    def main_branch(self) -> str:
        return self._main

    # --- 底层 git 调用 ---
    def _git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=str(self._dir),
            capture_output=True, text=True,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
        return proc.stdout

    def _record(self, action: str, **kw: Any) -> None:
        self.plan.append({"action": action, "dry_run": not self._enabled, **kw})

    # --- 分支 ---
    def current_branch(self) -> str:
        if not self._enabled:
            return self._main
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def checkout_new_branch(self, branch: str) -> GitResult:
        self._record("create_branch", branch=branch, base=self._main)
        if not self._enabled:
            return GitResult(True, note=f"[dry-run] 计划从 {self._main} 切分支 {branch}")
        self._git("checkout", "-b", branch)
        return GitResult(True, output=branch, note="created")

    def checkout(self, branch: str) -> GitResult:
        if not self._enabled:
            return GitResult(True, note=f"[dry-run] 计划切回 {branch}")
        self._git("checkout", branch)
        return GitResult(True, output=branch)

    # --- 改动落盘 + 提交 ---
    def apply_changes(self, files: dict[str, str]) -> GitResult:
        """把 {相对路径: 内容} 写入工作树。

        F-E.7:Edit 自改代码纳管当前项目全量代码,但写路径受 check_write_path
        白/黑名单兜底——data//.env/.git/ 等绝不允许被改写,越界/穿越路径直接拒绝。
        被拒文件不落盘,原因记入 plan 留痕(防御纵深:即便上游放过也在此拦截)。
        """
        from backend.orchestrator.tools import filter_writable

        allowed, denied = filter_writable(files)
        if denied:
            self._record("write_denied", files=sorted(denied.keys()),
                         reasons=denied)
        self._record("apply_changes", files=sorted(allowed.keys()))
        if not self._enabled:
            note = f"[dry-run] 计划写 {len(allowed)} 个文件"
            if denied:
                note += f"(拒写 {len(denied)} 个越界/黑名单文件)"
            return GitResult(True, note=note)
        for rel, content in allowed.items():
            p = self._dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        note = f"已写 {len(allowed)} 个文件"
        if denied:
            note += f"(拒写 {len(denied)} 个越界/黑名单文件)"
        return GitResult(True, note=note)

    def commit(self, message: str, files: list[str] | None = None) -> GitResult:
        self._record("commit", message=message.splitlines()[0], files=files)
        if not self._enabled:
            return GitResult(True, note="[dry-run] 计划提交", output=message)
        if files:
            self._git("add", *files)
        else:
            self._git("add", "-A")
        self._git("commit", "-m", message)
        sha = self._git("rev-parse", "HEAD").strip()
        return GitResult(True, output=sha, note="committed")

    # --- diff(回归 + 可视化高亮)---
    def diff(self, base: str | None = None, head: str | None = None) -> str:
        base = base or self._main
        if not self._enabled:
            planned = [a for a in self.plan if a["action"] == "apply_changes"]
            files = sorted({f for a in planned for f in a.get("files", [])})
            return "\n".join(f"+ (planned change) {f}" for f in files)
        head = head or "HEAD"
        return self._git("diff", f"{base}...{head}", check=False)

    # --- 回滚 ---
    def revert(self, commit: str = "HEAD") -> GitResult:
        self._record("revert", commit=commit)
        if not self._enabled:
            return GitResult(True, note=f"[dry-run] 计划 git revert {commit}")
        self._git("revert", "--no-edit", commit)
        return GitResult(True, note=f"reverted {commit}")

    # --- PR(受控:默认不自动推远端)---
    def open_pr(self, branch: str, summary: str,
                badcase_ref: str = "") -> PRDraft:
        diff_text = self.diff(self._main, branch if self._enabled else None)
        if self.can_push:
            # 真实推送 + 建 PR 的占位实现:推 feature 分支,PR 走 Host 在 GitHub 上确认。
            # 真正调用 GitHub API 需 Host 显式开启;这里默认仍不擅自建远端 PR。
            self._record("push", branch=branch)
            self._git("push", "-u", "origin", branch, check=False)
            pr_url = f"{self._remote.rstrip('.git')}/compare/{self._main}...{branch}?expand=1"
            return PRComposer.compose(branch, summary, diff_text, badcase_ref,
                                      pr_url=pr_url, pushed=True, dry_run=False)
        # dry-run:产出本地 PR 描述 + 占位 url,等 Host 确认后再推。
        self._record("open_pr", branch=branch, summary=summary)
        pr_url = f"{self._remote.rstrip('.git')}/compare/{self._main}...{branch}?expand=1"
        return PRComposer.compose(branch, summary, diff_text, badcase_ref,
                                  pr_url=pr_url, pushed=False, dry_run=True)
