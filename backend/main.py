"""进程入口:一处装配 OrchestratorService,多渠道复用(M4 入口层)。

  - FastAPI(uvicorn):Web 渠道 + 界面 API(POST /command、GET /events SSE…)。
  - 飞书长连接(LarkAdapter):出站 WS,免公网回调;后台守护线程跑。

两个入口共用同一个 OrchestratorService 实例(同一 EventBus/DecisionGate/Repo),
所以 Web 与飞书互通:飞书发起的任务也能在 Web 界面看事件流,反之亦然。

用法:
  env -u ARK_MODEL .venv/bin/python -m backend.main         # 默认 web + lark
  OPC_ENABLE_LARK=0 .venv/bin/python -m backend.main         # 只起 Web
"""
from __future__ import annotations

import os

import uvicorn

from backend.api.app import create_app
from backend.config import settings
from backend.orchestrator.service import OrchestratorService


def _truthy(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    service = OrchestratorService()

    # 飞书长连接:有凭据且未显式关闭则起后台线程
    enable_lark = _truthy(os.getenv("OPC_ENABLE_LARK"), default=True)
    if enable_lark and settings.lark_app_id and settings.lark_app_secret:
        from backend.gateway.lark_adapter import LarkAdapter

        adapter = LarkAdapter(service)
        adapter.start()
        print("[main] 飞书长连接已启动(出站 WebSocket)")
    else:
        print("[main] 飞书长连接未启用(缺 LARK_APP_ID/SECRET 或 OPC_ENABLE_LARK=0)")

    app = create_app(service)
    print(f"[main] API 监听 http://{settings.api_host}:{settings.api_port}")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
