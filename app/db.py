"""SQLite 连接与初始化。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = current_app.config["DATABASE"]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            db_path,
            timeout=15,
            isolation_level=None,  # 手动控制事务
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    """建表；多个 gunicorn worker 同时启动时对锁做退避重试。"""
    conn = get_db()
    script = SCHEMA_PATH.read_text(encoding="utf-8")
    delay = 0.05
    for attempt in range(10):
        try:
            conn.executescript("BEGIN IMMEDIATE;" + script + "COMMIT;")
            return
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.rollback()
            if "locked" not in str(exc).lower() or attempt == 9:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 立即拿写锁，避免并发下的丢失更新。"""
    if conn.in_transaction:
        # 不应出现；回滚残留事务以免把未完成的工作误提交。
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
