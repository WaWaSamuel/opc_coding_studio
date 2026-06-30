"""SqliteRepo:Repository 的 SQLite 实现(F-F.2 / M08)。

表:checkpoints / logs / daily_tokens(M1 子集;
artifacts/testcases/memory_* 等留后续里程碑)。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from backend.repo.repository import Repository
from backend.schema import CompanyState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    task_id     TEXT PRIMARY KEY,
    state_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    entry_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_tokens (
    day         TEXT PRIMARY KEY,
    tokens      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS artifacts (
    ref         TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_ns ON memory(namespace);
CREATE TABLE IF NOT EXISTS testcases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT UNIQUE,
    source      TEXT NOT NULL,
    intent      TEXT NOT NULL DEFAULT 'runtime',
    goal        TEXT NOT NULL,
    acceptance  TEXT NOT NULL DEFAULT '[]',
    origin_task TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_testcases_active ON testcases(active);
"""


class SqliteRepo(Repository):
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # 并行节点(F-B.1)会从多线程写同一连接;SQLite 连接非线程安全,
        # 用一把锁串行化所有读写(简单稳妥;高并发再换连接池/WAL)。
        self._lock = threading.Lock()

    # --- Checkpoint ---
    def save_checkpoint(self, state: CompanyState) -> None:
        from datetime import datetime, timezone

        with self._lock:
            self._conn.execute(
                "INSERT INTO checkpoints(task_id, state_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET state_json=excluded.state_json, "
                "updated_at=excluded.updated_at",
                (
                    state.task_id,
                    state.model_dump_json(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()

    def load_checkpoint(self, task_id: str) -> CompanyState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM checkpoints WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return CompanyState.model_validate_json(row["state_json"])

    def list_checkpoints(self, limit: int = 100) -> list[dict[str, Any]]:
        """历史任务列表(F-A.12):最近更新优先,提取轻量摘要供前端历史面板。

        不反序列化整份 state(只取可视化所需字段),避免大 state 拖慢列表。
        title 取首条 host 消息 / payload.intent 兜底,供历史列表一眼识别。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, state_json, updated_at FROM checkpoints "
                "ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                st = json.loads(r["state_json"])
            except (ValueError, TypeError):
                st = {}
            payload = st.get("payload") or {}
            messages = payload.get("messages") or []
            title = str(payload.get("text") or "").strip()
            if not title:
                for m in messages:
                    if (m or {}).get("role") == "host" and (m or {}).get("text"):
                        title = str(m["text"]).strip()
                        break
            if not title:
                title = str(payload.get("intent") or st.get("workflow") or "").strip()
            out.append({
                "task_id": r["task_id"],
                "system": st.get("system", "runtime"),
                "status": st.get("status", "running"),
                "title": title[:80],
                "updated_at": r["updated_at"],
            })
        return out

    # --- Logs ---
    def append_log(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs(task_id, entry_json) VALUES(?,?)",
                (entry.get("task_id", ""), json.dumps(entry, ensure_ascii=False)),
            )
            self._conn.commit()

    def logs_for(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_json FROM logs WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [json.loads(r["entry_json"]) for r in rows]

    # --- Daily tokens(成本熔断) ---
    def add_daily_tokens(self, day: str, tokens: int) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO daily_tokens(day, tokens) VALUES(?,?) "
                "ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens",
                (day, tokens),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT tokens FROM daily_tokens WHERE day=?", (day,)
            ).fetchone()
        return int(row["tokens"]) if row else 0

    def get_daily_tokens(self, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT tokens FROM daily_tokens WHERE day=?", (day,)
            ).fetchone()
        return int(row["tokens"]) if row else 0

    # --- Artifact 落库 / 取回(F-C.3 demand-paging) ---
    def save_artifact(self, task_id: str, ref: str, content: str) -> None:
        from datetime import datetime, timezone

        with self._lock:
            self._conn.execute(
                "INSERT INTO artifacts(ref, task_id, content, created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(ref) DO UPDATE SET "
                "content=excluded.content",
                (ref, task_id, content,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def load_artifact(self, ref: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM artifacts WHERE ref=?", (ref,)
            ).fetchone()
        return row["content"] if row else None

    # --- 长期记忆(命名空间隔离 + 关键词检索) ---
    def save_memory(self, namespace: str, kind: str, text: str) -> None:
        from datetime import datetime, timezone

        with self._lock:
            self._conn.execute(
                "INSERT INTO memory(namespace, kind, text, created_at) "
                "VALUES(?,?,?,?)",
                (namespace, kind, text,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def search_memory(
        self, namespace: str, query: str, top_k: int
    ) -> list[dict[str, Any]]:
        """关键词回退检索(F-C.5):取本命名空间全部条目,按 query 词命中数排序。

        向量检索(sqlite-vec + Ark embedding)是可选增强,本期默认关键词回退;
        命名空间严格隔离 runtime/edit(F-C.4),不跨库串味。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, text, created_at FROM memory WHERE namespace=? "
                "ORDER BY id DESC",
                (namespace,),
            ).fetchall()
        terms = [t for t in query.lower().split() if t]
        scored: list[tuple[int, dict[str, Any]]] = []
        for r in rows:
            text_lower = r["text"].lower()
            score = sum(text_lower.count(t) for t in terms) if terms else 0
            scored.append((score, {
                "kind": r["kind"], "text": r["text"],
                "created_at": r["created_at"], "score": score,
            }))
        # 有命中按分排序;全 0(无 query/无命中)按时间倒序保留最近
        hits = [item for s, item in scored if s > 0]
        hits.sort(key=lambda d: d["score"], reverse=True)
        if hits:
            return hits[:top_k]
        return [item for _, item in scored][:top_k]

    # --- 测试集(M10:Badcase 入库 + 每周回归)---
    def save_testcase(self, case: dict[str, Any]) -> int:
        from datetime import datetime, timezone

        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO testcases"
                "(dedup_key, source, intent, goal, acceptance, origin_task, "
                " active, created_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(dedup_key) DO NOTHING",
                (
                    case.get("dedup_key"),
                    case.get("source", "manual"),
                    case.get("intent", "runtime"),
                    case.get("goal", ""),
                    json.dumps(case.get("acceptance", []), ensure_ascii=False),
                    case.get("origin_task", ""),
                    1 if case.get("active", True) else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_testcases(self, only_active: bool = True) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, dedup_key, source, intent, goal, acceptance, "
            "origin_task, active, created_at FROM testcases"
        )
        if only_active:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r["id"], "dedup_key": r["dedup_key"],
                "source": r["source"], "intent": r["intent"],
                "goal": r["goal"],
                "acceptance": json.loads(r["acceptance"] or "[]"),
                "origin_task": r["origin_task"],
                "active": bool(r["active"]), "created_at": r["created_at"],
            })
        return out

    def has_testcase(self, dedup_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM testcases WHERE dedup_key=?", (dedup_key,)
            ).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
