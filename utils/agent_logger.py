"""
Agent Logger — утилита для записи сообщений агентов в pipeline.db.
"""
import os
import sqlite3
from datetime import datetime, timezone
from threading import Lock

_lock = Lock()
_run_id: str | None = None


def set_run_id(run_id: str) -> None:
    global _run_id
    _run_id = run_id


def log_message(
    sender: str,
    message: str,
    receiver: str | None = None,
    run_id: str | None = None,
) -> None:
    """Записывает сообщение агента в таблицу agent_messages."""
    rid = run_id or _run_id
    db_path = os.getenv("DB_PATH", "pipeline.db")
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with _lock:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "INSERT INTO agent_messages (timestamp, sender, receiver, message, run_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (ts, sender, receiver, message, rid),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass  # логгер не должен ломать пайплайн
