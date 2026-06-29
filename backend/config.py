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
    api_port: int = 8000
    cors_allow_origins: str = "*"   # 前端开发期放开;生产收敛到具体域名

    # 人在环决策(F-A.7):need_decision 编排线程阻塞等 Host 回灌的最长时间。
    # 超时则保守置 need_decision 终止本轮,不无限挂线程。
    decision_timeout_seconds: float = 3600.0


settings = Settings()
