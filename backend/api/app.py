"""FastAPI 应用工厂(M4 / F-A.3 Web 渠道 + 界面 API)。

路由(对齐 PRD 5.5):
  POST /command           下达指令(Web HostCommand)→ {task_id}
  GET  /events?task_id=   订阅事件流(SSE,text/event-stream)
  POST /decision          人在环回灌 {task_id,verdict,reason,suggestion}
  GET  /task/{id}         任务全量快照(CompanyState)
  GET  /cost?task_id=     成本聚合(按角色 tokens/latency)
  GET  /task/{id}/events  已落库流转事件回放(F-A.4)
  GET  /health            存活探针

事件流统一信封见 EventBus.Event;SSE 收到 done/error 后由 EventBus 推哨兵收流。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.config import settings
from backend.core.event_bus import EventBus
from backend.gateway.host_command import HostCommand, classify_intent
from backend.orchestrator.service import OrchestratorService


class CommandIn(BaseModel):
    text: str
    session_id: str = ""
    channel: str = "web"
    intent: Optional[str] = None     # 不传则按文本初判 runtime/edit
    host_verified: bool = True       # Web 默认信任(沙箱前置网关鉴权)
    attachments: list[dict[str, Any]] = []
    reply_to: str = ""


class DecisionIn(BaseModel):
    task_id: str
    verdict: str                     # pass | reject | abort
    reason: str = ""
    suggestion: str = ""


def create_app(service: OrchestratorService | None = None) -> FastAPI:
    svc = service or OrchestratorService()
    app = FastAPI(title="OPC Studio API", version="m4")
    app.state.service = svc

    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/command")
    def command(body: CommandIn) -> dict[str, str]:
        intent = body.intent or classify_intent(body.text)
        session_id = body.session_id or f"web-{id(body)}"
        cmd = HostCommand(
            channel="web", session_id=session_id, text=body.text,
            host_verified=body.host_verified, intent=intent,  # type: ignore[arg-type]
            attachments=body.attachments, reply_to=body.reply_to,
        )
        try:
            task_id = svc.submit(cmd)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"task_id": task_id, "intent": intent}

    @app.get("/events")
    async def events(task_id: str, request: Request) -> EventSourceResponse:
        q = svc.subscribe(task_id)

        async def gen():
            try:
                # 先补发已落库的历史事件(SSE 断线重连/晚订阅也能看到全程)。
                # 若任务已收口(历史含 done/error),哨兵已被早先订阅者消费,
                # 这里补发完即收流,避免晚订阅者在 live 循环里空等。
                already_done = False
                for past in svc.history(task_id):
                    yield {"event": past.get("event", "message"),
                           "data": json.dumps(past, ensure_ascii=False)}
                    if past.get("event") in ("done", "error"):
                        already_done = True
                if already_done:
                    return
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.to_thread(q.get, True, 1.0)
                    except Exception:  # queue.Empty:心跳保活
                        yield {"event": "ping", "data": "{}"}
                        continue
                    if EventBus.is_sentinel(item):
                        break
                    yield {"event": item.get("event", "message"),
                           "data": json.dumps(item, ensure_ascii=False)}
            finally:
                svc.unsubscribe(task_id, q)

        return EventSourceResponse(gen())

    @app.post("/decision")
    def decision(body: DecisionIn) -> dict[str, Any]:
        ok = svc.decide(body.task_id, body.verdict, body.reason, body.suggestion)
        return {"ok": ok, "task_id": body.task_id}

    @app.get("/task/{task_id}")
    def task(task_id: str) -> dict[str, Any]:
        snap = svc.task_snapshot(task_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="task not found")
        return snap

    @app.get("/task/{task_id}/events")
    def task_events(task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "events": svc.history(task_id)}

    @app.get("/cost")
    def cost(task_id: str) -> dict[str, Any]:
        return svc.cost(task_id)

    return app
