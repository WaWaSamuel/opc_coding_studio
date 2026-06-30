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
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.config import settings
from backend.core.event_bus import EventBus
from backend.gateway.host_command import (
    HostCommand,
    classify_decision,
    classify_intent,
)
from backend.orchestrator.service import OrchestratorService

# F-A.10 多模态图片上传落点(大图走 POST /upload,小图前端内联 base64)。
_UPLOAD_DIR = Path(settings.db_path).resolve().parent / "uploads"
_ALLOWED_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
})
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 单图上限 10MB


class CommandIn(BaseModel):
    text: str
    session_id: str = ""
    channel: str = "web"
    intent: Optional[str] = None     # 不传则按文本初判 runtime/edit
    host_verified: bool = True       # Web 默认信任(沙箱前置网关鉴权)
    attachments: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []  # F-A.9 多轮上下文(同 session 历史消息)
    reply_to: str = ""


class DecisionIn(BaseModel):
    task_id: str
    verdict: str = ""                # pass | reject | abort;留空则按 text 解析
    reason: str = ""
    suggestion: str = ""
    text: str = ""                   # F-A.7 通道①:对话式自然语言回复


class EditPrIn(BaseModel):
    branch: str
    summary: str = ""
    badcase_ref: str = ""


class RestartIn(BaseModel):
    scope: str = "both"              # backend | frontend | both


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
            attachments=body.attachments, messages=body.messages,
            reply_to=body.reply_to,
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
        # F-A.7 通道①:Host 直接打字回复时,verdict 留空 → 用 classify_decision
        # 把自然语言归一为 pass|reject|abort;无法判定返回 400 交前端按钮兜底。
        verdict = body.verdict or (classify_decision(body.text) or "")
        if verdict not in ("pass", "reject", "abort"):
            raise HTTPException(
                status_code=400,
                detail="无法从回复解析出决策,请用按钮或更明确的措辞(通过/打回/终止)",
            )
        reason = body.reason or body.text
        ok = svc.decide(body.task_id, verdict, reason, body.suggestion)
        return {"ok": ok, "task_id": body.task_id, "verdict": verdict}

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

    # --- 多模态图片上传(M6 / F-A.10)---
    @app.post("/upload")
    async def upload(file: UploadFile) -> dict[str, Any]:
        """接收图片 → 落 data/uploads/ → 返回可回访 url(供 attachments 透传)。

        仅允许图片类型;超 10MB 拒绝。返回 {url, name, content_type, size}。
        """
        ctype = (file.content_type or "").lower()
        if ctype not in _ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=415,
                                detail=f"仅支持图片类型,收到 {ctype or 'unknown'}")
        data = await file.read()
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="图片超过 10MB 上限")
        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
               "image/gif": ".gif", "image/webp": ".webp"}.get(ctype, ".bin")
        name = f"{uuid.uuid4().hex}{ext}"
        (_UPLOAD_DIR / name).write_bytes(data)
        return {"url": f"/uploads/{name}", "name": file.filename or name,
                "content_type": ctype, "size": len(data)}

    @app.get("/uploads/{name}")
    def serve_upload(name: str) -> FileResponse:
        """回访已上传图片(前端预览 / 多模态模型取图)。防目录穿越。"""
        safe = Path(name).name
        path = _UPLOAD_DIR / safe
        if safe != name or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(str(path))

    # --- Edit 系统(M5 / F-A.8 可视化 + F-E.4 受控 PR + M6 F-E.6 自重启)---
    @app.get("/edit/graph")
    def edit_graph(ref: str = "main", workflow: str = "edit") -> dict[str, Any]:
        """工作流静态 DAG(M6/F-A.8 多工作流全景)。

        workflow ∈ {edit, runtime};feature ref 标改动节点做 diff 高亮。
        node.role_id 供前端 RoleInspector 下钻角色详情。
        """
        return svc.edit_graph(ref=ref, workflow=workflow)

    @app.get("/role/{role_id}")
    def role_detail(role_id: str) -> dict[str, Any]:
        """角色完整元数据(M6/F-A.8 RoleInspector):model_tier/职责/可调 tool。"""
        detail = svc.role_detail(role_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="role not found")
        return detail

    @app.post("/edit/restart")
    def edit_restart(body: RestartIn) -> dict[str, Any]:
        """服务自重启(M6/F-E.6)。闸门关 → 回 restart_required 信号(dry-run)。"""
        return svc.restart_service(body.scope)

    @app.post("/edit/pr")
    def edit_pr(body: EditPrIn) -> dict[str, Any]:
        """提 PR(受控:默认 dry-run,不擅自推远端)→ {pr_url,...}。"""
        return svc.submit_edit_pr(body.branch, body.summary, body.badcase_ref)

    @app.post("/edit/testsuite/run")
    def edit_testsuite_run() -> dict[str, Any]:
        """手动跑一遍回归测试集(F-E.3)→ 通过率报告。"""
        return svc.run_testsuite()

    @app.post("/edit/testsuite/seed")
    def edit_testsuite_seed() -> dict[str, int]:
        """G7 测试集冷启动:载入种子用例。"""
        return {"added": svc.load_seed_testcases()}

    return app
