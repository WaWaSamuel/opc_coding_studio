"""TestSuiteRunner + EvalReporter:每周回归单测(M10 / F-E.3)。

对测试集(种子用例 + Badcase 入库用例)跑回归,算通过率;<阈值(默认 95%)
则告警 Host 并可触发 Edit 优化。评估目前仅"通过率"(质量评分留 G1)。

回归怎么"跑":把每条用例的 goal 经 Runtime 串行编排重放一遍,看是否跑到 DONE
且交付物文本命中验收标准。为保证离线确定性与可测,case_runner 可注入;
默认 runtime_case_runner 用调用方提供的 NodeRunner 工厂在进程内同步跑 RuntimeGraph。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from backend.config import settings
from backend.orchestrator.graph_runtime import RuntimeGraph
from backend.orchestrator.loop import LoopController
from backend.repo.repository import Repository
from backend.schema import CompanyState, TaskStatus

CaseRunner = Callable[[dict[str, Any]], "CaseResult"]


@dataclass
class CaseResult:
    case_id: Any
    goal: str
    passed: bool
    note: str = ""


@dataclass
class EvalReport:
    total: int
    passed: int
    pass_rate: float
    threshold: float
    below_threshold: bool
    results: list[CaseResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "threshold": self.threshold,
            "below_threshold": self.below_threshold,
            "results": [
                {"case_id": r.case_id, "goal": r.goal,
                 "passed": r.passed, "note": r.note}
                for r in self.results
            ],
        }


class EvalReporter:
    """把逐例结果汇总为通过率报告;判定是否低于阈值。"""

    def __init__(self, threshold: float | None = None) -> None:
        self._threshold = (
            settings.eval_pass_threshold if threshold is None else threshold
        )

    def report(self, results: list[CaseResult]) -> EvalReport:
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        rate = (passed / total) if total else 1.0
        return EvalReport(
            total=total, passed=passed, pass_rate=rate,
            threshold=self._threshold,
            below_threshold=(total > 0 and rate < self._threshold),
            results=results,
        )


class TestSuiteRunner:
    __test__ = False  # 非 pytest 测试类(名字以 Test 开头,显式排除收集)

    def __init__(
        self,
        repo: Repository,
        case_runner: CaseRunner,
        threshold: float | None = None,
    ) -> None:
        self._repo = repo
        self._run_case = case_runner
        self._reporter = EvalReporter(threshold)

    def run(self, only_active: bool = True) -> EvalReport:
        cases = self._repo.list_testcases(only_active=only_active)
        results = [self._run_case(c) for c in cases]
        return self._reporter.report(results)


def runtime_case_runner(runner_factory: Callable[[], Any]) -> CaseRunner:
    """构造默认回归执行器:在进程内同步跑 RuntimeGraph(无事件总线/线程)。

    runner_factory:返回一个全新 NodeRunner(独立 memory namespace,复用 repo)。
    判定通过 = 跑到 TaskStatus.DONE 且(若给了 acceptance)交付物文本逐条命中。
    """

    def _run(case: dict[str, Any]) -> CaseResult:
        goal = case.get("goal", "")
        case_id = case.get("id", case.get("dedup_key", "?"))
        try:
            state = CompanyState(
                task_id=f"regression-{case_id}", system="runtime",
                workflow="regression",
            )
            graph = RuntimeGraph(runner_factory(), loop=LoopController())
            result = graph.run(state, goal)
            if result.status != TaskStatus.DONE:
                return CaseResult(case_id, goal, False,
                                  note=f"status={result.status.value}")
            acceptance = case.get("acceptance", []) or []
            if acceptance:
                blob = _result_text(result).lower()
                missing = [a for a in acceptance if not _hit(blob, a)]
                if missing:
                    return CaseResult(case_id, goal, False,
                                      note=f"未命中验收: {missing}")
            return CaseResult(case_id, goal, True, note="done")
        except Exception as exc:  # noqa: BLE001 — 单例失败不应炸整套回归
            return CaseResult(case_id, goal, False,
                              note=f"{type(exc).__name__}: {exc}")

    return _run


def _result_text(result: Any) -> str:
    parts: list[str] = []
    fa = getattr(result, "final_artifact", None)
    if fa is not None:
        parts.append(fa.artifact.summary or "")
        parts.extend(fa.artifact.files or [])
    for art in getattr(result.state, "artifacts", []):
        parts.append(art.artifact.summary or "")
        parts.extend(art.artifact.files or [])
    return " ".join(parts)


def _hit(blob: str, acceptance: str) -> bool:
    """验收命中判定:整句命中,或关键词(去标点切分)多数命中。"""
    a = acceptance.lower().strip()
    if not a:
        return True
    if a in blob:
        return True
    import re

    terms = [t for t in re.split(r"[\s、,，。:：/]+", a) if len(t) >= 2]
    if not terms:
        return False
    hits = sum(1 for t in terms if t in blob)
    return hits >= max(1, len(terms) // 2)
