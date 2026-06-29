"""G7 种子测试用例:测试集冷启动(M10 边界)。

Badcase Agent 入库前测试集为空,无法跑回归。这里给一组覆盖典型业务流的种子
用例,Scheduler 首跑/Host 手动 seed 时载入测试集,作为每周回归的基线锚点。
"""
from __future__ import annotations

from typing import Any

from backend.repo.repository import Repository

# 每条:goal(宿主业务目标)+ acceptance(可被产物文本逐条命中的验收标准)。
# dedup_key 以 seed: 前缀,避免与 Badcase 入库用例冲突。
SEED_CASES: list[dict[str, Any]] = [
    {
        "dedup_key": "seed:ecommerce-order",
        "source": "seed",
        "intent": "runtime",
        "goal": "搭建一个最小电商下单接口:商品列表 + 下单 + 订单查询。",
        "acceptance": ["提供商品列表接口", "提供下单与订单查询接口"],
    },
    {
        "dedup_key": "seed:blog-crud",
        "source": "seed",
        "intent": "runtime",
        "goal": "实现一个博客后端:文章的增删改查接口。",
        "acceptance": ["提供文章创建接口", "提供文章列表与详情接口", "提供文章删除接口"],
    },
    {
        "dedup_key": "seed:todo-api",
        "source": "seed",
        "intent": "runtime",
        "goal": "实现一个待办事项服务:新增待办、标记完成、列出待办。",
        "acceptance": ["提供新增待办接口", "提供标记完成接口", "提供待办列表接口"],
    },
]


def load_seed_testcases(repo: Repository) -> int:
    """把种子用例写入测试集;已存在(按 dedup_key)则跳过。返回新增条数。"""
    added = 0
    for case in SEED_CASES:
        if repo.has_testcase(case["dedup_key"]):
            continue
        repo.save_testcase(case)
        added += 1
    return added
