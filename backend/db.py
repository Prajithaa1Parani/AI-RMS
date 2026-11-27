"""SQLite database: schema, small query helpers, settings and audit log."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student','staff','admin')),
    department TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id),
    original_text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    clarifications TEXT NOT NULL DEFAULT '[]',
    pending_questions TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    category TEXT,
    priority TEXT,
    department TEXT,
    fields TEXT NOT NULL DEFAULT '{}',
    suggested_response TEXT,
    unavailable_info TEXT NOT NULL DEFAULT '[]',
    ai_recommended_action TEXT,
    verification TEXT,
    rule_notes TEXT NOT NULL DEFAULT '[]',
    injection_flag INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    staff_response TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    actor TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    user_id INTEGER NOT NULL,
    ticket_id INTEGER,
    status TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ticket_id INTEGER,
    user_id INTEGER,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    user_id INTEGER,
    task TEXT NOT NULL,
    model_key TEXT,
    provider TEXT,
    model TEXT,
    attempt INTEGER,
    outcome TEXT NOT NULL,
    error TEXT,
    selection_reason TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    prompt_version TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SEED_USERS = [
    ("student1", "student123", "Aarav Sharma", "student", None),
    ("student2", "student123", "Meera Nair", "student", None),
    ("hostel_staff", "staff123", "Hostel Office Staff", "staff", "hostel_office"),
    ("accounts_staff", "staff123", "Accounts Staff", "staff", "accounts"),
    ("exam_staff", "staff123", "Examination Cell Staff", "staff", "examination_cell"),
    ("it_staff", "staff123", "IT Helpdesk Staff", "staff", "it_helpdesk"),
    ("admin", "admin123", "RMS Administrator", "admin", None),
]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction():
    """All statements inside succeed together or are rolled back together."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql, args=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def query_one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=()):
    with transaction() as conn:
        return conn.execute(sql, args).lastrowid


def init_db():
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    for username, password, name, role, dept in SEED_USERS:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, full_name, role, department)"
            " VALUES (?,?,?,?,?)",
            (username, generate_password_hash(password), name, role, dept),
        )
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                     (key, json.dumps(value)))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- settings
def get_settings():
    out = dict(config.DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        out[row["key"]] = json.loads(row["value"])
    return out


def set_setting(key, value):
    execute("INSERT INTO settings (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)))


# ---------------------------------------------------------------- audit
def audit(stage, status, detail=None, run_id=None, ticket_id=None, user_id=None):
    """Write one audit record. `detail` is any JSON-serialisable object."""
    execute(
        "INSERT INTO audit_logs (run_id, ticket_id, user_id, stage, status, detail, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, ticket_id, user_id, stage, status,
         json.dumps(detail, default=str) if detail is not None else None, now()),
    )


# ---------------------------------------------------------------- spend
def spend_today(user_id):
    row = query_one("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_calls"
                    " WHERE user_id = ? AND created_at >= date('now')", (user_id,))
    return row["s"]


def spend_total():
    return query_one("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_calls")["s"]
