"""SQLite persistence layer. Every engagement, file, extraction, match, finding and
change is stored so the working paper can be re-performed and traced."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','partner','manager','auditor','viewer')),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engagements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  module TEXT NOT NULL DEFAULT 'sales_vouching',
  client_name TEXT NOT NULL,
  financial_year TEXT NOT NULL,
  fy_start TEXT NOT NULL,
  fy_end TEXT NOT NULL,
  tolerance REAL NOT NULL DEFAULT 1.0,
  materiality REAL,
  cutoff_days INTEGER NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT 'open',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finalized_by TEXT,
  finalized_at TEXT
);
CREATE TABLE IF NOT EXISTS fx_rates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id INTEGER NOT NULL REFERENCES engagements(id),
  currency TEXT NOT NULL,
  rate REAL NOT NULL,
  source TEXT NOT NULL,
  entered_by TEXT NOT NULL,
  entered_at TEXT NOT NULL,
  UNIQUE (engagement_id, currency)
);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id INTEGER NOT NULL REFERENCES engagements(id),
  kind TEXT NOT NULL CHECK (kind IN ('template','ledger','invoice','output')),
  original_name TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  uploaded_by TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  meta TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id INTEGER NOT NULL REFERENCES engagements(id),
  file_id INTEGER NOT NULL REFERENCES files(id),
  status TEXT NOT NULL DEFAULT 'queued',
  doc_type TEXT,
  language TEXT,
  page_count INTEGER,
  has_text_layer INTEGER,
  extraction_method TEXT,
  overall_confidence REAL,
  fields TEXT,
  ai_fields TEXT,
  line_items TEXT,
  extraction_notes TEXT,
  fx_override REAL,
  manual TEXT,
  match TEXT,
  review_status TEXT NOT NULL DEFAULT 'pending',
  reviewer TEXT,
  reviewed_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inv_eng ON invoices(engagement_id);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id INTEGER NOT NULL REFERENCES engagements(id),
  file_id INTEGER NOT NULL REFERENCES files(id),
  row_number INTEGER NOT NULL,
  invoice_number TEXT,
  customer TEXT,
  date TEXT,
  amount REAL,
  currency TEXT,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_led_eng ON ledger_entries(engagement_id);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id INTEGER NOT NULL REFERENCES engagements(id),
  invoice_id INTEGER,
  ledger_id INTEGER,
  code TEXT NOT NULL,
  observation TEXT NOT NULL,
  risk TEXT NOT NULL,
  reason TEXT NOT NULL,
  document TEXT,
  procedure TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'open',
  reviewer_note TEXT,
  reviewed_by TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (engagement_id, invoice_id, ledger_id, code)
);
CREATE TABLE IF NOT EXISTS customer_aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id INTEGER NOT NULL REFERENCES engagements(id),
  invoice_name TEXT NOT NULL,
  ledger_name TEXT NOT NULL,
  confirmed_by TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  UNIQUE (engagement_id, invoice_name, ledger_name)
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  user TEXT NOT NULL,
  ts TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  user TEXT NOT NULL,
  engagement_id INTEGER,
  entity TEXT NOT NULL,
  entity_id INTEGER,
  action TEXT NOT NULL,
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_eng ON audit_log(engagement_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = _connect()
        _local.conn = c
    return c


def init() -> None:
    c = _connect()
    c.executescript(SCHEMA)
    c.commit()
    c.close()


@contextmanager
def tx():
    with _write_lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn().execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def jload(v: str | None, default=None):
    if v in (None, ""):
        return default
    return json.loads(v)


def jdump(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def log(user: str, engagement_id: int | None, entity: str, entity_id: int | None, action: str,
        field: str | None = None, old=None, new=None, note: str | None = None, c: sqlite3.Connection | None = None) -> None:
    sql = ("INSERT INTO audit_log(ts,user,engagement_id,entity,entity_id,action,field,old_value,new_value,note) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)")
    args = (now(), user, engagement_id, entity, entity_id, action, field,
            None if old is None else jdump(old), None if new is None else jdump(new), note)
    if c is not None:
        c.execute(sql, args)
    else:
        with tx() as cc:
            cc.execute(sql, args)
