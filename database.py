"""Disposable SQLite fixtures. No application path opens the legacy database."""
import sqlite3
from datetime import datetime, timezone

TABLES = ("customers", "orders", "cloud_volumes")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", isolation_level=None, cached_statements=0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA trusted_schema = OFF")
    db.execute("PRAGMA temp_store = MEMORY")
    db.execute("PRAGMA max_page_count = 256")
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16384)
    db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192)
    db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 40)
    return db


def new_database() -> sqlite3.Connection:
    db = empty_database()
    db.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
            plan TEXT NOT NULL, signed_up_at TEXT NOT NULL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
            amount_cents INTEGER NOT NULL, status TEXT NOT NULL, placed_at TEXT NOT NULL
        );
        CREATE TABLE cloud_volumes (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, size_gb INTEGER NOT NULL,
            attached_to TEXT, has_backups INTEGER NOT NULL DEFAULT 1
        );
    """)
    plans = ["free", "starter", "pro", "enterprise"]
    now = timestamp()
    db.executemany(
        "INSERT INTO customers VALUES (?,?,?,?,?)",
        ((i, f"Customer {i}", f"user{i}@pocketos.example", plans[i % 4], now) for i in range(1, 251)),
    )
    db.executemany(
        "INSERT INTO orders VALUES (?,?,?,?,?)",
        ((i, i % 250 + 1, 1999 + (i * 37) % 9000, "paid", now) for i in range(1, 1201)),
    )
    db.executemany("INSERT INTO cloud_volumes VALUES (?,?,?,?,?)", [
        ("vol_prod_db_01", "pocketos-prod-db", 500, "db-01.prod", 1),
        ("vol_staging_01", "pocketos-staging", 50, "db-01.staging", 0),
    ])
    db.commit()
    return db


def stats(db: sqlite3.Connection) -> dict:
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    return {
        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in present else None
        for table in TABLES
    }
