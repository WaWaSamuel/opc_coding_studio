"""Loop 规则先行硬校验(F-B.7 ①:规则/可执行校验先行,机械客观)。

在把判定交给模型语义评判之前,先跑一批可执行的硬性校验,任一项不过直接 reject,
不进模型(把模型主观判定权重压到最小)。对应 PRD:"硬约束先行拦掉客观缺陷"。

M2 范围说明:此时执行角色的交付物是「结构化描述」(文件清单 + 摘要),
尚无真实文件内容/可运行代码,因此硬校验只覆盖**从 Artifact 本身可机械判定**的项:
  - status 必须为 done(blocked/need_rework 直接 reject)
  - 必须声明产出文件(files 非空)
  - 无残留 open_questions(有未决问题不算交付完成)
PRD acceptance 的「逐条命中」属于语义/可执行验证:在没有真实产物可执行前,
交给语义判定角色(loop-judge / qa-acceptance);M3 接入 Tool/沙箱后,
再在此补"编译/lint/单测/契约/acceptance 命中"等真·可执行校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.schema import Artifact, ArtifactStatus


@dataclass
class RuleResult:
    passed: bool
    failed_checks: list[str] = field(default_factory=list)


def run_rule_checks(artifact: Artifact) -> RuleResult:
    """对执行角色的 Artifact 跑机械硬校验(M2:结构性可判定项)。"""
    failed: list[str] = []

    if artifact.status != ArtifactStatus.DONE:
        failed.append(f"status={artifact.status.value}(非 done)")

    if not artifact.artifact.files:
        failed.append("未声明任何产出文件(files 为空)")

    if artifact.open_questions:
        failed.append(f"存在未决问题 {len(artifact.open_questions)} 项")

    return RuleResult(passed=not failed, failed_checks=failed)
