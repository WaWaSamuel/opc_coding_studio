"""SessionRouter:会话↔任务映射 + Host 身份校验(M01 / F-A.1)。

每个渠道会话(飞书 chat / Web 连接)映射一个 session_id;一个 session
对应一条活跃 task_id,支持多轮。Host 校验来源为渠道身份:飞书用
target_open_id 白名单,Web 默认信任本地(生产可换鉴权中间件)。
"""
from __future__ import annotations

import threading
import uuid


class SessionRouter:
    """session_id ↔ 活跃 task_id 双向映射(线程安全)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_to_task: dict[str, str] = {}
        self._task_to_session: dict[str, str] = {}

    def new_task_id(self, session_id: str, prefix: str = "task") -> str:
        """为会话开一条新任务,登记映射并返回 task_id。"""
        task_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._session_to_task[session_id] = task_id
            self._task_to_session[task_id] = session_id
        return task_id

    def bind(self, session_id: str, task_id: str) -> None:
        with self._lock:
            self._session_to_task[session_id] = task_id
            self._task_to_session[task_id] = session_id

    def task_for(self, session_id: str) -> str | None:
        with self._lock:
            return self._session_to_task.get(session_id)

    def session_for(self, task_id: str) -> str | None:
        with self._lock:
            return self._task_to_session.get(task_id)


class HostAuthorizer:
    """Host 身份校验(F-A.1 非 Host 来源直接拒绝)。

    飞书:target_open_id 为空 → 不限制(本地调试);否则只接受白名单 open_id。
    Web:本期默认信任(沙箱不开对外端口,前置部署/网关负责鉴权)。
    """

    def __init__(self, lark_target_open_id: str = "") -> None:
        self._lark_target = (lark_target_open_id or "").strip()

    def verify_lark(self, sender_open_id: str) -> bool:
        if not self._lark_target:
            return True
        return sender_open_id == self._lark_target

    def verify_web(self) -> bool:
        return True
