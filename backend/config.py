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

    # Persistence (F-F.2)
    db_path: str = "./data/app.db"

    # Cost Guard (F-D.6)
    soft_task_tokens: int = 50_000
    hard_task_tokens: int = 100_000
    max_daily_tokens: int = 2_000_000

    # Reliability (F-D.3)
    max_self_repair: int = 3
    max_api_overload_retry: int = 3


settings = Settings()
