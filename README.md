# 一人公司 AI Agent 系统(OPC Studio)

> 无状态角色函数 + Runtime / Edit 双系统编排的「一人公司」AI Agent 系统(PRD v4.2)。
> Host 一个人发指令,系统内多个 AI 角色按流转(Handoff)协作交付,全程留痕、硬约束、保命阀兜底。

远端真源:`https://github.com/WaWaSamuel/opc_coding_studio.git`

---

## 这是什么

- **Runtime 系统**:接业务需求 → CEO 路由 → 部长拆解 TODO → 工程执行 → 回归/验收 → 汇总交付。
- **Edit 系统(自迭代)**:让系统改进系统自身 —— 定位+TODO → feature diff → 回归 ≥95%(硬约束)→ 提 PR 等 Host 确认 Merge,异常 `git revert` 回滚。
- **入口多渠道**:Web 界面(React)+ 飞书长连接(出站 WebSocket,免公网回调),共用同一编排实例,互通事件流。
- **保命默认**:模型 `mock` 离线可跑;Edit 真实改动/推远端/调度器默认全关,需 Host 显式开启 + 凭据就绪才生效。

里程碑 M1~M5 已交付,能力清单见 [backend/README.md](file:///Users/bytedance/StickerProductive/OPC_Studio/backend/README.md)。

---

## 快速开始(本地裸跑)

全部指令已收口到 [scripts/opc.sh](file:///Users/bytedance/StickerProductive/OPC_Studio/scripts/opc.sh),在仓库根目录执行:

```bash
./scripts/opc.sh install            # 建 .venv、装后端+前端依赖、从 .env.example 生成 .env
./scripts/opc.sh run                # 后台起后端 → http://localhost:8001
./scripts/opc.sh run --with-frontend # 同时起 vite dev → http://localhost:5174
./scripts/opc.sh status             # 查看运行状态
./scripts/opc.sh logs -f            # 跟随后端日志(logs frontend -f 看前端)
./scripts/opc.sh stop               # 停止本地后台进程
./scripts/opc.sh test               # 全量单测(强制 mock 离线确定性)
./scripts/opc.sh build              # 前端生产构建 → frontend/dist
```

> 默认 `MODEL_PROVIDER=mock`,无需任何密钥即可起服务、跑测试。
> 接真实豆包 / 飞书 / GitHub:编辑根目录 `.env`(已 gitignore)填入对应密钥后重启。

### 脚本子命令一览

| 命令 | 作用 |
|---|---|
| `install` | 创建 `.venv`、装 `backend/requirements.txt` 与 `frontend` 依赖、生成 `.env` |
| `run [--with-frontend]` | 后台起后端;加 `--with-frontend` 同时起前端 dev |
| `stop` | 停止后端 / 前端后台进程 |
| `restart [...]` | 先 stop 再 run(透传 run 参数) |
| `logs [backend\|frontend] [-f]` | 查看日志,默认后端;`-f` 跟随 |
| `status` | 后端/前端运行状态与端口 |
| `test [...]` | 全量单测(`env -u ARK_MODEL -u MODEL_PROVIDER pytest -q`,透传 pytest 参数) |
| `build` | 前端 `tsc -b && vite build` |
| `docker-up` / `docker-down` / `docker-logs [svc]` | 容器模式编排 |

运行态产物:进程 pid 在 `.run/`,日志在 `logs/`(均已 gitignore)。

---

## 容器化运行(需 Docker)

```bash
./scripts/opc.sh docker-up      # = docker compose up --build -d
./scripts/opc.sh docker-logs    # 跟随两服务日志
./scripts/opc.sh docker-down    # 停止并清理
```

- `backend`:FastAPI + 编排 + 飞书长连接,端口 `8001`,持久化挂载 `./data`、`./workflows`。
- `frontend`:Vite 构建产物经 nginx 静态服务,端口 `5174`,`/api` 反代到 backend。
- 打开 `http://localhost:5174` 即用 Web 界面;有 `LARK_*` 凭据时飞书长连接随 backend 自动建立。

> 容器读根目录 `.env`(`env_file`),先 `cp .env.example .env` 填密钥(`install` 已自动生成)。

---

## 目录结构

```
OPC_Studio/
├── scripts/opc.sh        # 运维脚本:安装/运行/停止/日志/测试/构建/容器(本次收口)
├── backend/              # FastAPI + 编排内核(M1~M5),入口 backend/main.py
│   ├── api/              #   FastAPI 路由(/command /events SSE /decision /edit/* …)
│   ├── orchestrator/     #   Runtime/Edit 状态图、NodeRunner、重试、并行、决策回灌
│   ├── core/             #   ModelAdapter、成本熔断、记忆/检索、角色 specs、EventBus
│   ├── services/         #   GitService、TestSuite、Badcase、Scheduler、种子用例(M5)
│   ├── gateway/          #   入口层:飞书长连接、Web 命令归一、会话路由
│   └── repo/             #   SQLite 持久化 + Checkpoint
├── frontend/             # React + Vite 界面(Chat/流转留痕/TODO/成本/决策/GraphView)
├── tests/                # 全量单测(M1~M5,离线确定性)
├── workflows/            # 业务工作流定义(git 真源)
├── docker-compose.yml    # backend + frontend 两服务编排
└── .env.example          # 环境变量样例(cp 为 .env 填真实密钥)
```

---

## 配置与密钥纪律

- 所有密钥仅经环境变量(根目录 `.env`)注入;`.env`、`data/`、`logs/`、`.run/` 均 gitignore,绝不入库。
- 仓库只放 `.env.example` 占位。完整配置项见 [.env.example](file:///Users/bytedance/StickerProductive/OPC_Studio/.env.example)。

关键开关(均为保命默认,需显式开启):

| 变量 | 默认 | 含义 |
|---|---|---|
| `MODEL_PROVIDER` | `mock` | `mock` 离线 / `ark` 接豆包 |
| `OPC_ENABLE_LARK` | `1` | 有 `LARK_*` 凭据时连飞书;`0` 只起 Web |
| `EDIT_GIT_ENABLED` | `0` | Edit 真实改动开关;`0`=dry-run 只产 diff/PR 描述 |
| `EDIT_PUSH_ENABLED` | `0` | 是否允许真实 push/建 PR 到远端(不可逆) |
| `SCHEDULER_ENABLED` | `0` | 是否自动起每周回归调度器 |
| `EVAL_PASS_THRESHOLD` | `0.95` | 回归通过率硬约束阈值 |

---

## 测试

```bash
./scripts/opc.sh test           # 全量
./scripts/opc.sh test tests/test_m5_edit.py   # 透传 pytest 参数,跑单文件
```

等价于在仓库根目录执行 `env -u ARK_MODEL -u MODEL_PROVIDER .venv/bin/python -m pytest -q`,
强制 mock、屏蔽 shell 残留的 `ARK_*`,保证离线确定性。
