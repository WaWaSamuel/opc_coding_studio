"""配置:仅从环境变量读取,密钥不入库(PRD F-F.4 / 密钥纪律)。"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", protected_namespaces=()
    )

    # Model (F-F.1)
    model_provider: str = "mock"  # mock | ark
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_model: str = ""
    ark_model_small: str = ""
    ark_timeout_seconds: float = 300.0  # 单次模型调用读超时(慢端点/长输出留余量)

    # Persistence (F-F.2)
    db_path: str = "./data/app.db"

    # Cost Guard (F-D.6)
    soft_task_tokens: int = 50_000
    hard_task_tokens: int = 100_000
    max_daily_tokens: int = 2_000_000

    # Reliability (F-D.3)
    max_self_repair: int = 3
    max_api_overload_retry: int = 3

    # Business loop / rework (F-D.1 业务回退上限,超限置 need_decision)
    max_loop_iterations: int = 3

    # Node retry (F-D.2 节点重试:瞬时失败原地重试,与业务回退独立计数)
    max_node_retry: int = 3

    # Global circuit breakers (M07,借 Claude Code:每条自动恢复路径配上限)
    max_compact_failures: int = 3   # 连续 Compact 失败 → 停压缩报 Host

    # Context / Memory (F-C.3/C.5,M05)
    compact_trigger_tokens: int = 8_000   # 任务记忆超此阈值触发全量压缩
    result_preview_bytes: int = 2_048     # 大产出落库后 prompt 内保留预览字节数
    retrieve_top_k: int = 3               # 检索注入 Top-K

    # Gateway 飞书入口(M01 / F-A.2,M4)。长连接(出站 WS)无需公网回调。
    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_domain: str = "https://open.feishu.cn"
    # 只接受该 Host 的指令(身份校验,F-A.1);为空则不限制(本地调试)。
    lark_bot_target_open_id: str = ""

    # API 服务(M4 入口层 / 界面)
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    cors_allow_origins: str = "*"   # 前端开发期放开;生产收敛到具体域名

    # 人在环决策(F-A.7):need_decision 编排线程阻塞等 Host 回灌的最长时间。
    # 超时则保守置 need_decision 终止本轮,不无限挂线程。
    decision_timeout_seconds: float = 3600.0

    # ── M5 自迭代 + 版本管理(域 E / M09 / M10)─────────────────
    # 远端 GitHub 唯一真源(F-E.4)。Token 仅经环境变量,不入库/日志/git。
    github_token: str = ""
    github_repo: str = "https://github.com/WaWaSamuel/opc_coding_studio.git"
    git_main_branch: str = "master"
    # Edit 真实改动开关:False 时 GitService 走 dry-run(只产出 diff/PR 描述,
    # 不动真实仓库),保命默认。需 Host 显式开启 + GITHUB_TOKEN 就绪才真实推送。
    edit_git_enabled: bool = False
    # 是否允许真实 push/建 PR 到远端(F-E.4 不可逆动作的兜底闸门)。
    # 默认 False:本地 git + 受控 PR,push/PR 走 Host 确认,不擅自推远端。
    edit_push_enabled: bool = False
    # 服务自重启(F-E.6):Edit 改了 backend/** 或 frontend/** 且经回归+Merge 后,
    # 是否允许自动重启前后端使改动生效。默认 False:只发 restart_required 信号,
    # 由 Host 手动重启(自重启脱离当前请求进程,health 失败回滚,属高危,保命默认关)。
    edit_auto_restart_enabled: bool = False

    # 每周单测阈值(F-E.3):回归成功率 < 该值 → 告警 Host 并触发 Edit。
    eval_pass_threshold: float = 0.95
    # Scheduler(F-E.5):每周 Badcase 单测间隔(秒,默认 7 天);0 关闭。
    scheduler_weekly_seconds: float = 7 * 24 * 3600.0
    scheduler_enabled: bool = False  # 服务启动是否自动起调度器(测试默认关)


settings = Settings()
