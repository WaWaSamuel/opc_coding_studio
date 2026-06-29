"""LarkAdapter:飞书长连接入口(M01 / F-A.2)。

用 lark-oapi 出站 WebSocket(长连接)接入飞书,免公网回调:
  ① 订阅 im.message.receive_v1:把宿主消息归一为 HostCommand 投编排器;
  ② 订阅 card.action.trigger:把卡片按钮回调等价为人在环回灌 svc.decide;
  ③ 订阅 EventBus:把编排事件流式更新回飞书交互卡片(进度/成本/need_decision)。

硬约束(飞书长连接语义):
  - 事件回调须 3 秒内返回,否则触发超时重推 → 工作流一律丢后台线程,
    回调内只做"归一 + svc.submit + 起流式线程"后立即返回。
  - event_id 幂等去重:重推的同一事件不重复开任务。
  - 仅企业自建应用支持长连接;身份校验复用 svc.auth.verify_lark。
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from backend.config import settings
from backend.core.event_bus import EventBus
from backend.gateway.host_command import HostCommand, classify_intent
from backend.orchestrator.service import OrchestratorService

# 事件类型 → 中文里程碑标签(卡片进度行展示)
_EVENT_LABEL = {
    "graph_start": "受理需求",
    "ceo_route": "CEO 路由分流",
    "handoff": "棒次流转",
    "dev_plan": "部长拆解 TODO",
    "build": "执行交付",
    "rule_check": "硬约束校验",
    "loop_judge": "质量判定",
    "rework": "业务回退重做",
    "acceptance": "需求验收",
    "need_decision": "等待宿主决策",
    "decision": "宿主已拍板",
    "cost_soft_limit": "成本预警",
    "done": "收口完成",
    "error": "异常中止",
}


class LarkAdapter:
    """飞书长连接适配器:进程内单实例,生命周期与服务一致。"""

    def __init__(self, service: OrchestratorService) -> None:
        self._svc = service
        self._bus: EventBus = service.bus
        # im.v1.message.create/patch 用的 REST 客户端(与长连接客户端分开)
        self._client = (
            lark.Client.builder()
            .app_id(settings.lark_app_id)
            .app_secret(settings.lark_app_secret)
            .domain(settings.lark_domain)
            .build()
        )
        # event_id 幂等去重(有界 LRU,防内存膨胀)
        self._seen_lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_cap = 4096
        self._ws: lark.ws.Client | None = None

    # ── 启动 ───────────────────────────────────────────────────
    def start(self) -> threading.Thread:
        """在后台守护线程里跑长连接(client.start() 是阻塞的)。"""
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )
        self._ws = lark.ws.Client(
            settings.lark_app_id,
            settings.lark_app_secret,
            event_handler=handler,
            domain=settings.lark_domain,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )

        def _run() -> None:
            self._ws.start()  # 阻塞:内部跑事件循环 + 自动重连

        t = threading.Thread(target=_run, name="lark-ws", daemon=True)
        t.start()
        return t

    # ── 去重 ───────────────────────────────────────────────────
    def _is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        with self._seen_lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = None
            if len(self._seen) > self._seen_cap:
                self._seen.popitem(last=False)
            return False

    # ── 消息接收回调(必须 3 秒内返回)──────────────────────────
    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            event_id = ""
            if data.header is not None:
                event_id = data.header.event_id or ""
            if self._is_duplicate(event_id):
                return

            msg = data.event.message
            sender = data.event.sender
            open_id = ""
            if sender is not None and sender.sender_id is not None:
                open_id = sender.sender_id.open_id or ""
            chat_id = msg.chat_id or ""

            # F-A.1 Host 身份校验:非授权来源直接忽略
            if not self._svc.auth.verify_lark(open_id):
                self._send_text(chat_id, "⛔ 非授权宿主,已忽略本条指令。")
                return

            text = self._extract_text(msg.message_type or "", msg.content or "")
            if not text.strip():
                self._send_text(chat_id, "请发送文本指令(暂仅支持纯文本)。")
                return

            cmd = HostCommand(
                channel="lark",
                session_id=chat_id,
                text=text,
                host_verified=True,
                intent=classify_intent(text),
                reply_to=chat_id,
                raw={"event_id": event_id, "open_id": open_id,
                     "message_id": msg.message_id or ""},
            )
            task_id = self._svc.submit(cmd)
            # 工作流已在 svc.submit 后台线程跑;这里再起流式线程把事件回灌飞书卡片
            threading.Thread(
                target=self._stream_to_card,
                args=(task_id, chat_id, text),
                name=f"lark-stream-{task_id}",
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001 — 回调内绝不抛,避免连接被拖垮
            lark.logger.error(f"[lark] on_message failed: {exc}")

    # ── 卡片按钮回调 = 人在环回灌(等价 POST /decision)──────────
    def _on_card_action(
        self, data: P2CardActionTrigger
    ) -> P2CardActionTriggerResponse:
        try:
            action = data.event.action if data.event else None
            value: dict[str, Any] = (action.value or {}) if action else {}
            task_id = str(value.get("task_id", ""))
            verdict = str(value.get("verdict", ""))
            if task_id and verdict:
                self._svc.decide(task_id, verdict, reason="飞书卡片按钮")
                toast = {"type": "info", "content": f"已收到决策:{verdict}"}
            else:
                toast = {"type": "error", "content": "无效的决策回调"}
        except Exception as exc:  # noqa: BLE001
            lark.logger.error(f"[lark] on_card_action failed: {exc}")
            toast = {"type": "error", "content": "决策处理异常"}
        return P2CardActionTriggerResponse({"toast": toast})

    # ── 把编排事件流式更新回飞书卡片 ────────────────────────────
    def _stream_to_card(self, task_id: str, chat_id: str, goal: str) -> None:
        q = self._svc.subscribe(task_id)
        lines: list[str] = []
        message_id = self._send_card(chat_id, self._render_card(goal, lines, False))
        try:
            while True:
                try:
                    item = q.get(True, 30.0)
                except Exception:  # queue.Empty:保活,不更新
                    continue
                if EventBus.is_sentinel(item):
                    break
                event = item.get("event", "")
                label = _EVENT_LABEL.get(event)
                if label is None:
                    continue  # 只渲染里程碑,节点级细粒度事件不刷屏
                lines.append(self._format_line(label, item))
                need_decision = event == "need_decision"
                done = event in ("done", "error")
                if message_id:
                    self._patch_card(
                        message_id,
                        self._render_card(goal, lines, need_decision,
                                          task_id=task_id, done=done),
                    )
                if done:
                    break
        finally:
            self._svc.unsubscribe(task_id, q)

    # ── 渲染 ───────────────────────────────────────────────────
    @staticmethod
    def _format_line(label: str, item: dict[str, Any]) -> str:
        payload = item.get("payload", {}) or {}
        note = ""
        if "verdict" in payload:
            note = f"(判定:{payload['verdict']})"
        elif "department" in payload:
            note = f"(部门:{payload['department']})"
        elif "note" in payload:
            note = f"— {str(payload['note'])[:60]}"
        elif "error" in payload:
            note = f"— {str(payload['error'])[:80]}"
        return f"✅ **{label}** {note}".rstrip()

    def _render_card(
        self,
        goal: str,
        lines: list[str],
        need_decision: bool,
        task_id: str = "",
        done: bool = False,
    ) -> dict[str, Any]:
        template = "red" if need_decision else ("green" if done else "blue")
        title = "一人公司 · 任务进行中"
        if need_decision:
            title = "一人公司 · 等待你拍板"
        elif done:
            title = "一人公司 · 已收口"
        body = "\n".join(lines) if lines else "已受理,正在编排…"
        elements: list[dict[str, Any]] = [
            {"tag": "div",
             "text": {"tag": "lark_md", "content": f"**需求**:{goal[:120]}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
        ]
        if need_decision and task_id:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "action",
                "actions": [
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "放行收口"},
                     "type": "primary",
                     "value": {"task_id": task_id, "verdict": "pass"}},
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "打回返工"},
                     "type": "default",
                     "value": {"task_id": task_id, "verdict": "reject"}},
                    {"tag": "button",
                     "text": {"tag": "plain_text", "content": "终止任务"},
                     "type": "danger",
                     "value": {"task_id": task_id, "verdict": "abort"}},
                ],
            })
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        }

    # ── 飞书消息收发(REST)──────────────────────────────────────
    def _send_text(self, chat_id: str, text: str) -> None:
        if not chat_id:
            return
        try:
            self._client.im.v1.message.create(
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
        except Exception as exc:  # noqa: BLE001
            lark.logger.error(f"[lark] send_text failed: {exc}")

    def _send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        if not chat_id:
            return ""
        try:
            resp = self._client.im.v1.message.create(
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(json.dumps(card, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            if resp.success() and resp.data is not None:
                return resp.data.message_id or ""
            lark.logger.error(f"[lark] send_card failed: {resp.msg}")
        except Exception as exc:  # noqa: BLE001
            lark.logger.error(f"[lark] send_card failed: {exc}")
        return ""

    def _patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        try:
            self._client.im.v1.message.patch(
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(json.dumps(card, ensure_ascii=False))
                    .build()
                )
                .build()
            )
        except Exception as exc:  # noqa: BLE001
            lark.logger.error(f"[lark] patch_card failed: {exc}")

    @staticmethod
    def _extract_text(message_type: str, content: str) -> str:
        """从飞书消息 content(JSON 串)取纯文本。"""
        if not content:
            return ""
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return ""
        if message_type == "text":
            return str(data.get("text", "")).strip()
        if message_type == "post":  # 富文本:拼接所有 text 段
            parts: list[str] = []
            post = data.get("post") or data
            for lang in (post.values() if isinstance(post, dict) else []):
                for line in (lang.get("content", []) if isinstance(lang, dict) else []):
                    for seg in line:
                        if isinstance(seg, dict) and seg.get("tag") == "text":
                            parts.append(seg.get("text", ""))
            return " ".join(parts).strip()
        return ""
