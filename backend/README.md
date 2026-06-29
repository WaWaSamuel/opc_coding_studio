# 一人公司 AI Agent 系统 — Backend

PRD v4.2 **M1 最小内核 + M2 业务流 PoC** 实现。

> - **M1**(保命内核):`ModelAdapter(豆包+JSON强约束) + SQLite 状态落盘/Checkpoint + 单节点 NodeRunner + Artifact schema + token 记账与软硬熔断`。
>   目标:一个角色被调一次 → 产出合法 Artifact → 断点可恢复 → token 被记账并能熔断。
> - **M2**(价值验证):用最朴素**串行编排**端到端跑通一条电商业务流,证明角色协作能交付可用产物。
>   `CEO 路由 → 开发部长拆解 → 后端执行 → Loop 判定(规则先行+模型兜底)→ 业务回退(≤3)→ 需求验收 → 部长汇总`。
> - M3+(记忆/Compact、并行、入口界面、自迭代)尚未实现。

## 已实现功能点

| 编号 | 功能点 | 里程碑 | 落点 |
|---|---|---|---|
| F-F.1 | Model Adapter(豆包 Ark,可切换) | M1 | `core/model_adapter/` |
| F-F.2 | SQLite 本地持久化(接口抽象) | M1 | `repo/` |
| F-D.3 | JSON 强约束 + 自修复 | M1 | `core/model_adapter/`、`orchestrator/node_runner.py` |
| F-D.4 | DB 持久化 + Checkpoint(断点恢复) | M1 | `repo/checkpoint_store.py` |
| F-D.6 | 成本熔断(token 软硬 + 日限) | M1 | `core/cost_guard.py` |
| F-C.1 | 三层提示词(公共/角色/任务 + CACHE_BOUNDARY) | M1 | `core/roles/prompt_composer.py` |
| F-B.9 | 结构化 Artifact 驱动 | M1 | `schema.py` |
| F-B.10 | 无状态角色调用 + 上下文隔离 | M1 | `orchestrator/node_runner.py` |
| F-A.6 | token/成本记账 | M1 | `core/cost_guard.py`、`repo` logs |
| F-B.1 | Runtime 状态图(串行版) | M2 | `orchestrator/graph_runtime.py` |
| F-B.3 | CEO 路由分流 | M2 | `core/roles/specs/ceo-orchestrator-agent.yaml` |
| F-B.4 | 部长拆解 + TODO Plan + 验收汇总 | M2 | `core/roles/specs/dev-lead-agent.yaml` |
| F-B.6 | 开发执行 / 验收角色 | M2 | `specs/backend-engineer-agent.yaml`、`qa-acceptance-agent.yaml` |
| F-B.7 | Loop 判定(规则先行 + 模型语义兜底) | M2 | `orchestrator/rule_checks.py`、`loop-judge-agent.yaml` |
| F-D.1 | 业务回退(Loop/Rework ≤3,超限 need_decision) | M2 | `orchestrator/loop.py`、`edges.py` |
| F-A.4 | 流转事件(graph/ceo_route/build/loop_judge/rework/…) | M2 | `orchestrator/graph_runtime.py` event_sink |

## 运行(在仓库根目录 `OPC_Studio/` 下执行)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# M1 单角色离线闭环(默认 MockAdapter,无需密钥)
python -m backend.main

# 接真实豆包:设置环境变量后 provider=ark
export ARK_API_KEY=... ARK_BASE_URL=... ARK_MODEL=...
MODEL_PROVIDER=ark python -m backend.main

# M2 电商业务流 PoC(端到端串行编排;建议用真实 ark 跑全链路)
MODEL_PROVIDER=ark python -m backend.run_ecommerce
```

## 测试(仓库根目录)

```bash
pytest -q   # M1(test_m1_kernel)+ M2(test_m2_runtime),全部离线确定性
```

## 密钥纪律

所有密钥仅经环境变量注入,`data/` 与 `.env` 已 gitignore。仓库只放 `.env.example` 占位。
