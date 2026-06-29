"""JSON 自修复(F-D.3)。

解析模型输出为目标 schema;失败时把错误回喂模型修正,计入 retry,
达 MAX_SELF_REPAIR 仍失败抛 SelfRepairExhausted。
"""
from __future__ import annotations

import json
from typing import Callable

from backend.config import settings
from backend.errors import SchemaViolation, SelfRepairExhausted
from backend.schema import Artifact, InvokeResult


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
        # 去掉可能的 ```json 行残留
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    return t.strip()


def parse_artifact(content: str) -> Artifact:
    """把模型文本解析为 Artifact,失败抛 SchemaViolation。"""
    raw = _strip_fence(content)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaViolation(f"非合法 JSON: {exc}") from exc
    try:
        return Artifact.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise SchemaViolation(f"不符合 Artifact schema: {exc}") from exc


def parse_with_self_repair(
    first: InvokeResult,
    reinvoke: Callable[[str], InvokeResult],
    retry_counters: dict[str, int],
) -> tuple[Artifact, list[InvokeResult]]:
    """解析,失败则回喂错误自修复。

    reinvoke(error_msg) 返回新的 InvokeResult。
    返回 (artifact, 所有调用结果)用于成本记账。
    """
    results = [first]
    content = first.content
    last_err = ""
    for attempt in range(settings.max_self_repair):
        try:
            return parse_artifact(content), results
        except SchemaViolation as exc:
            last_err = str(exc)
            retry_counters["self_repair"] = retry_counters.get("self_repair", 0) + 1
            res = reinvoke(
                f"上次输出解析失败:{last_err}。请只输出一个合法 JSON 对象,"
                f"严格符合 Artifact schema,不要任何额外文字或代码围栏。"
            )
            results.append(res)
            content = res.content
    raise SelfRepairExhausted(f"自修复 {settings.max_self_repair} 次仍失败: {last_err}")
