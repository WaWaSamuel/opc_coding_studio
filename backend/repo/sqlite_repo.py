"""SqliteRepo:Repository 的 SQLite 实现(F-F.2 / M08)。

表:checkpoints / logs / daily_tokens(M1 子集;
artifacts/testcases/memory_* 等留后续里程碑)。
"""
from __future__ import annotations

import json
import sqlite3
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
"""


class SqliteRepo(Repository):
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- Checkpoint ---
    def save_checkpoint(self, state: CompanyState) -> None:
        from datetime import datetime, timezone

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
        row = self._conn.execute(
            "SELECT state_json FROM checkpoints WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return CompanyState.model_validate_json(row["state_json"])

    # --- Logs ---
    def append_log(self, entry: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO logs(task_id, entry_json) VALUES(?,?)",
            (entry.get("task_id", ""), json.dumps(entry, ensure_ascii=False)),
        )
        self._conn.commit()

    def logs_for(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT entry_json FROM logs WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return [json.loads(r["entry_json"]) for r in rows]

    # --- Daily tokens(成本熔断) ---
    def add_daily_tokens(self, day: str, tokens: int) -> int:
        self._conn.execute(
            "INSERT INTO daily_tokens(day, tokens) VALUES(?,?) "
            "ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens",
            (day, tokens),
        )
        self._conn.commit()
        return self.get_daily_tokens(day)

    def get_daily_tokens(self, day: str) -> int:
        row = self._conn.execute(
            "SELECT tokens FROM daily_tokens WHERE day=?", (day,)
        ).fetchone()
        return int(row["tokens"]) if row else 0

    def close(self) -> None:
        self._conn.close()
