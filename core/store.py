# -*- coding: utf-8 -*-
"""导入结果的持久化：SQLite，支持长期反复运行、断点重试与审计。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "imports.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_code TEXT NOT NULL,
  store_name TEXT,
  wave TEXT,
  status TEXT NOT NULL,
  message TEXT,
  matched INTEGER DEFAULT 0,
  total INTEGER DEFAULT 0,
  missing_required INTEGER DEFAULT 0,
  manual_required INTEGER DEFAULT 0,
  submitted INTEGER DEFAULT 0,
  created_at REAL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_code ON runs(store_code);
CREATE INDEX IF NOT EXISTS idx_runs_time ON runs(created_at);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def record_run(store_code: str, store_name: str, wave: str, status: str,
               message: str = "", matched: int = 0, total: int = 0,
               missing_required: int = 0, manual_required: int = 0,
               submitted: int = 0, detail: dict | None = None) -> int:
    with _lock:
        c = _get_conn()
        cur = c.execute(
            "INSERT INTO runs (store_code, store_name, wave, status, message, matched,"
            " total, missing_required, manual_required, submitted, created_at, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_code, store_name, wave, status, message, matched, total,
             missing_required, manual_required, submitted, time.time(),
             json.dumps(detail or {}, ensure_ascii=False)),
        )
        c.commit()
        return int(cur.lastrowid)


def latest_runs(limit: int = 500) -> list[dict]:
    with _lock:
        c = _get_conn()
        rows = c.execute(
            "SELECT r.* FROM runs r WHERE r.created_at = ("
            " SELECT MAX(created_at) FROM runs x WHERE x.store_code = r.store_code)"
            " ORDER BY r.created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def history(store_code: str, limit: int = 20) -> list[dict]:
    with _lock:
        c = _get_conn()
        rows = c.execute("SELECT * FROM runs WHERE store_code=? ORDER BY created_at DESC LIMIT ?",
                         (store_code, limit)).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    rows = latest_runs(limit=100000)
    out: dict[str, int] = {}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    out["_total"] = len(rows)
    return out


def clear() -> None:
    with _lock:
        c = _get_conn()
        c.execute("DELETE FROM runs")
        c.commit()


def get_meta(key: str, default=None):
    with _lock:
        c = _get_conn()
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(key: str, value) -> None:
    with _lock:
        c = _get_conn()
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))
        c.commit()
