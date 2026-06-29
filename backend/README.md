# 一人公司 AI Agent 系统 — Backend

PRD v4.2 **M1 最小内核(保命)** 实现。

> 范围严格对齐 PRD 第三部分 M1:`ModelAdapter(豆包+JSON强约束) + SQLite 状态落盘/Checkpoint + 单节点 NodeRunner + Artifact schema + token 记账与软硬熔断`。
> 目标:**一个角色被调一次 → 产出合法 Artifact → 断点可恢复 → token 被记账并能熔断**。
> M2+(业务流 PoC / harness 加固 / 入口界面 / 自迭代)尚未实现。

## 已实现功能点

| 编号 | 功能点 | 落点 |
|---|---|---|
| F-F.1 | Model Adapter(豆包 Ark,可切换) | `core/model_adapter/` |
| F-F.2 | SQLite 本地持久化(接口抽象) | `repo/` |
| F-D.3 | JSON 强约束 + 自修复 | `core/model_adapter/`、`orchestrator/node_runner.py` |
| F-D.4 | DB 持久化 + Checkpoint(断点恢复) | `repo/checkpoint_store.py` |
| F-D.6 | 成本熔断(token 软硬 + 日限) | `core/cost_guard.py` |
| F-C.1 | 三层提示词(公共/角色/任务 + CACHE_BOUNDARY) | `core/roles/prompt_composer.py` |
| F-B.9 | 结构化 Artifact 驱动 | `schema.py` |
| F-B.10 | 无状态角色调用 + 上下文隔离 | `orchestrator/node_runner.py` |
| F-A.6 | token/成本记账 | `core/cost_guard.py`、`repo` logs |

## 运行(在仓库根目录 `OPC_Studio/` 下执行)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 离线闭环(默认 MockAdapter,无需密钥)
python -m backend.main

# 接真实豆包:设置环境变量后 provider=ark
export ARK_API_KEY=... ARK_BASE_URL=... ARK_MODEL=...
MODEL_PROVIDER=ark python -m backend.main
```

## 测试(仓库根目录)

```bash
pytest -q
```

## 密钥纪律

所有密钥仅经环境变量注入,`data/` 与 `.env` 已 gitignore。仓库只放 `.env.example` 占位。
