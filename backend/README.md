# 一人公司 AI Agent 系统 — Backend

PRD v4.2 **M1 最小内核 + M2 业务流 PoC + M3 Harness 加固 + M4 产品化入口/界面** 实现。

> - **M1**(保命内核):`ModelAdapter(豆包+JSON强约束) + SQLite 状态落盘/Checkpoint + 单节点 NodeRunner + Artifact schema + token 记账与软硬熔断`。
>   目标:一个角色被调一次 → 产出合法 Artifact → 断点可恢复 → token 被记账并能熔断。
> - **M2**(价值验证):用最朴素**串行编排**端到端跑通一条电商业务流,证明角色协作能交付可用产物。
>   `CEO 路由 → 开发部长拆解 → 后端执行 → Loop 判定(规则先行+模型兜底)→ 业务回退(≤3)→ 需求验收 → 部长汇总`。
> - **M3**(加固为可长期运行):节点重试(与业务回退独立计数)、Tool 四层最小授权、无依赖节点并行 + 确定性 join、三层记忆 + 三级 Compact 流水线(大产出落库 demand-paging)、检索式记忆注入 + 稳定前缀缓存统计。
> - **M4**(产品化入口 + 界面):入口层 Gateway(飞书长连接 + Web)、EventBus 事件总线(内存 pub/sub + 落库)、FastAPI 服务(command/events SSE/decision/task/cost)、React 界面(Chat/流转留痕/TODO/成本/决策弹窗)、PM/PRD 角色、人在环 DecisionGate 回灌、容器化 docker-compose。
> - M5(Edit 自迭代、Git 服务、Scheduler)尚未实现。

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
| F-D.2 | 节点重试(瞬时失败原地重试,与业务回退独立计数) | M3 | `orchestrator/retry.py`、`node_runner.py` |
| F-D.5 | Tool 最小授权四层流(白名单/Hook/危险检测/Host 确认) | M3 | `orchestrator/tools.py` |
| F-B.1 | 无依赖节点并行 + 确定性 join(子状态深拷贝隔离) | M3 | `orchestrator/parallel.py` |
| F-C.2/C.3 | 三层记忆 + 三级 Compact(预算落库/微压缩/九段摘要+熔断) | M3 | `core/memory.py` |
| F-C.4 | 记忆命名空间隔离(runtime/edit) | M3 | `core/memory.py`、`repo/sqlite_repo.py` |
| F-C.5 | 检索式记忆注入(关键词回退,Top-K) | M3 | `core/retrieval.py`、`prompt_composer.py` |
| F-C.6 | 稳定前缀缓存命中率统计 | M3 | `core/retrieval.py` |
| F-D.6+ | 全局熔断常量表(每条自动恢复路径配上限) | M3 | `config.py`(max_node_retry/max_compact_failures) |
| F-A.1 | 入口层 Gateway(HostCommand 归一 + 会话路由 + Host 鉴权) | M4 | `gateway/host_command.py`、`session_router.py` |
| F-A.2 | 飞书机器人渠道(长连接出站 WS + 卡片决策回调) | M4 | `gateway/lark_adapter.py` |
| F-A.3 | Web 交互渠道(FastAPI + SSE 流式) | M4 | `api/app.py` |
| F-A.4 | 流转留痕看板(按 task_id 回放) | M4 | `api/app.py` `/task/{id}/events`、前端 EventTimeline |
| F-A.5 | TODO Plan 进度视图 | M4 | `frontend/src/components/TodoView.tsx` |
| F-A.7 | 人在环决策(need_decision 卡片/弹窗 + 回灌) | M4 | `orchestrator/decision_gate.py`、`api/app.py` `/decision` |
| F-B.5 | PM 部门 + 结构化 PRD 角色 | M4 | `core/roles/specs/pm-prd-agent.yaml` |
| F-F.3 | 事件流总线 EventBus(内存 pub/sub + 落库分离) | M4 | `core/event_bus.py` |
| F-F.4 | 进程入口一处装配 OrchestratorService(多渠道复用) | M4 | `main.py`、`orchestrator/service.py` |
| F-F.5 | 容器化(backend + frontend 两服务 compose) | M4 | `Dockerfile`、`../docker-compose.yml` |

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

# M3 Harness 加固能力(离线 demo,默认 MockAdapter,无需密钥)
# 逐项演示:节点重试 / Tool 四层授权 / 无依赖并行 join / 三层记忆 demand-paging / 前缀缓存
python -m backend.run_m3_harness

# M4 入口层服务(Web + 飞书长连接)。一处装配 OrchestratorService,多渠道复用。
# 默认 web + lark(有 LARK_* 凭据自动连飞书);只起 Web 用 OPC_ENABLE_LARK=0。
python -m backend.main          # → http://localhost:8000(/health /command /events SSE /decision /cost)
```

## M4 前端界面(React + Vite,在 `frontend/` 下)

```bash
cd frontend
npm install
npm run dev      # 开发期:http://localhost:5173,/api 自动代理到后端 8000
npm run build    # 产物到 frontend/dist(容器镜像构建用)
```

## M4 容器化(在仓库根目录,需 Docker)

```bash
cp .env.example .env          # 填入真实 ARK_* / LARK_* 等;.env 已 gitignore
docker compose up --build     # backend(8000)+ frontend(5173,nginx /api 反代后端)
# 打开 http://localhost:5173 即用 Web 界面;飞书长连接随 backend 自动建立(有凭据时)。
```

> 网络约束(PRD §5.7):沙箱内不得起对外监听;飞书长连接是**出站** WebSocket,
> 无需公网回调/内网穿透,因此在受限网络也能连上飞书。

## 测试(仓库根目录)

```bash
pytest -q   # M1(test_m1_kernel)+ M2(test_m2_runtime)+ M3(test_m3_*)+ M4(test_m4_gateway),全部离线确定性
```

## 密钥纪律

所有密钥仅经环境变量注入,`data/` 与 `.env` 已 gitignore。仓库只放 `.env.example` 占位。
