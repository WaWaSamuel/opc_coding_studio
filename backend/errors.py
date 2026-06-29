"""统一异常(对应 PRD 失败处理与熔断)。"""
from __future__ import annotations


class OpcError(Exception):
    """基类。"""


class SchemaViolation(OpcError):
    """模型输出不符合 JSON schema,触发自修复(F-D.3)。"""


class SelfRepairExhausted(OpcError):
    """自修复达上限仍失败(retry_counters 超限,error 上报)。"""


class CostLimitExceeded(OpcError):
    """成本熔断:任务/日 token 触硬限(F-D.6)。"""

    def __init__(self, scope: str, used: int, limit: int):
        self.scope = scope
        self.used = used
        self.limit = limit
        super().__init__(f"cost limit exceeded [{scope}]: used={used} limit={limit}")


class ModelCallError(OpcError):
    """模型 API 调用失败(5xx/限流,重试上限后上报)。"""
