"""SQLite demo database — seeds a fake SaaS and a fake cloud control plane."""
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "veylix.db")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db() -> None:
    con = _conn()
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            plan TEXT NOT NULL,
            signed_up_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL,
            placed_at TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        CREATE TABLE IF NOT EXISTS cloud_volumes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            size_gb INTEGER NOT NULL,
            attached_to TEXT,
            has_backups INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            classification TEXT NOT NULL,
            blast_radius TEXT,
            decision TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            action TEXT NOT NULL,
            classification TEXT NOT NULL,
            blast_radius TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        """
    )

    cur.execute("SELECT COUNT(*) AS n FROM customers")
    if cur.fetchone()["n"] == 0:
        plans = ["free", "starter", "pro", "enterprise"]
        for i in range(1, 251):
            cur.execute(
                "INSERT INTO customers (name, email, plan, signed_up_at) VALUES (?, ?, ?, ?)",
                (f"Customer {i}", f"user{i}@pocketos.example", plans[i % 4], datetime.utcnow().isoformat()),
            )
        for i in range(1, 1201):
            cur.execute(
                "INSERT INTO orders (customer_id, amount_cents, status, placed_at) VALUES (?, ?, ?, ?)",
                ((i % 250) + 1, 1999 + (i * 37) % 9000, "paid", datetime.utcnow().isoformat()),
            )
        cur.execute(
            "INSERT OR IGNORE INTO cloud_volumes (id, name, size_gb, attached_to, has_backups) VALUES (?,?,?,?,?)",
            ("vol_prod_db_01", "pocketos-prod-db", 500, "db-01.prod", 1),
        )
        cur.execute(
            "INSERT OR IGNORE INTO cloud_volumes (id, name, size_gb, attached_to, has_backups) VALUES (?,?,?,?,?)",
            ("vol_staging_01", "pocketos-staging", 50, "db-01.staging", 0),
        )

    con.commit()
    con.close()


def stats() -> dict:
    con = _conn()
    cur = con.cursor()
    out = {}
    for t in ("customers", "orders", "cloud_volumes"):
        cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
        out[t] = cur.fetchone()["n"]
    con.close()
    return out


def log_audit(action: str, classification: str, blast_radius: str, decision: str) -> None:
    con = _conn()
    con.execute(
        "INSERT INTO audit_log (ts, action, classification, blast_radius, decision) VALUES (?,?,?,?,?)",
        (datetime.utcnow().isoformat(), action, classification, blast_radius, decision),
    )
    con.commit()
    con.close()


def recent_audit(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def enqueue(kind: str, action: str, classification: str, blast_radius: str) -> int:
    con = _conn()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO pending_actions (created_at, kind, action, classification, blast_radius, status) VALUES (?,?,?,?,?,?)",
        (datetime.utcnow().isoformat(), kind, action, classification, blast_radius, "pending"),
    )
    con.commit()
    pid = cur.lastrowid
    con.close()
    return pid


def pending() -> list[dict]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM pending_actions WHERE status='pending' ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def resolve(pid: int, decision: str) -> dict | None:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM pending_actions WHERE id=?", (pid,))
    row = cur.fetchone()
    if row is None:
        con.close()
        return None
    cur.execute("UPDATE pending_actions SET status=? WHERE id=?", (decision, pid))
    con.commit()
    out = dict(row)
    out["status"] = decision
    con.close()
    return out
