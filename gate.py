"""Classify one proposal and execute only inside the session's fixtures."""
import json
import math
import re
import sqlite3
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from time import monotonic

import sqlglot
from sqlglot import exp

import database

MAX_ACTION = 2048
READ_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "hex", "ifnull", "instr", "length",
    "like", "lower", "ltrim", "max", "min", "nullif", "replace", "round", "rtrim",
    "substr", "sum", "total", "trim", "typeof", "upper",
})
SAFE_SHELL = {"ls", "ls -la", "pwd", "whoami", "git status", "git log --oneline"}


@dataclass(frozen=True)
class Decision:
    classification: str
    reason: str
    sql: str | None = None
    operation: int | None = None
    table: str | None = None
    impact: dict = field(default_factory=dict)


@dataclass
class Demo:
    db: sqlite3.Connection = field(default_factory=database.new_database)
    audit: deque = field(default_factory=lambda: deque(maxlen=100))
    pending: dict = field(default_factory=dict)
    next_id: int = 1
    revision: int = 0

    def log(self, kind: str, action: str, classification: str, decision: str, impact: dict):
        self.audit.appendleft({
            "ts": database.timestamp(), "kind": kind, "action": action,
            "classification": classification, "decision": decision, "blast_radius": impact,
        })


def blocked(reason: str) -> Decision:
    return Decision("blocked", reason)


def classify_shell(db: sqlite3.Connection, command: str) -> Decision:
    if command in SAFE_SHELL:
        return Decision("safe", "Allowlisted shell demonstration", impact={"simulated": True})
    volume = re.fullmatch(r"vultr volume delete ([A-Za-z0-9_-]{1,64})", command)
    if volume:
        target = volume[1]
        row = db.execute("SELECT * FROM cloud_volumes WHERE id=?", (target,)).fetchone()
        impact = {
            "simulated": True, "volume_id": target, "matched_fixture_volumes": int(row is not None),
            "backup_deletion": "Not modeled",
        }
        if row is not None:
            impact.update(volume_name=row["name"], size_gb=row["size_gb"],
                          attached_to=row["attached_to"], fixture_has_backups=bool(row["has_backups"]))
        return Decision("destructive", "Mock cloud volume deletion", impact=impact)
    if re.fullmatch(r"rm -rf? [A-Za-z0-9_./-]+", command):
        return Decision("destructive", "Simulated recursive removal",
                        impact={"simulated": True, "target": command.split()[-1], "files_at_risk": "Not modeled"})
    return blocked("Unsupported shell command or shell syntax")


def primary_key_scope(statement: exp.Expression) -> bool:
    where = statement.args.get("where")
    test = where.this if where else None
    return (
        isinstance(test, exp.EQ)
        and isinstance(test.left, exp.Column)
        and not test.left.table
        and test.left.name.lower() == "id"
        and isinstance(test.right, exp.Literal)
    )


def classify_sql(sql: str) -> Decision:
    try:
        statements = sqlglot.parse(sql, dialect="sqlite")
    except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
        return blocked("SQL could not be parsed")
    if len(statements) != 1:
        return blocked("Submit exactly one SQL statement")
    statement = statements[0]
    if isinstance(statement, exp.Select):
        return Decision("safe", "Read from the demo fixtures", sql)

    target = statement.this if statement is not None else None
    table = target.this if isinstance(target, exp.Schema) else target
    if (
        not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier)
        or table.db or table.catalog or table.alias or table.name.lower() not in database.TABLES
    ):
        return blocked("SQL must target one allowlisted demo table")
    name = table.name.lower()

    if isinstance(statement, exp.Drop):
        if not re.fullmatch(rf"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:{name}|\"{name}\")\s*;?", sql, re.I):
            return blocked("Only a plain DROP TABLE can be reviewed")
        return Decision("destructive", "Fixture table removal", sql, sqlite3.SQLITE_DROP_TABLE, name)
    if isinstance(statement, exp.Update):
        if any(value for key, value in statement.args.items() if key not in {"this", "expressions", "where"}):
            return blocked("Unsupported UPDATE syntax")
        scoped = primary_key_scope(statement) and all(
            isinstance(assignment, exp.EQ)
            and isinstance(assignment.left, exp.Column)
            and assignment.left.name.lower() not in {"id", "rowid", "_rowid_", "oid"}
            for assignment in statement.expressions
        )
        return Decision("reversible" if scoped else "destructive",
                        "Primary-key scoped UPDATE" if scoped else "UPDATE requires review",
                        sql, sqlite3.SQLITE_UPDATE, name)
    if isinstance(statement, exp.Delete):
        if any(value for key, value in statement.args.items() if key not in {"this", "where"}):
            return blocked("Unsupported DELETE syntax")
        scoped = primary_key_scope(statement)
        return Decision("reversible" if scoped else "destructive",
                        "Primary-key scoped DELETE" if scoped else "DELETE requires review",
                        sql, sqlite3.SQLITE_DELETE, name)
    if isinstance(statement, exp.Insert):
        if isinstance(statement.expression, exp.Values) and not any(
            value for key, value in statement.args.items() if key not in {"this", "expression"}
        ):
            return Decision("reversible", "Insert values into a demo table", sql, sqlite3.SQLITE_INSERT, name)
    return blocked("Unsupported SQL statement")


def run_sql(db: sqlite3.Connection, decision: Decision) -> dict:
    denied = False

    def authorize(code, first, second, schema, source):
        nonlocal denied
        # DROP is checked by an exact pattern and needs internal schema writes.
        # All other statements use this authorizer.
        if (
            code == sqlite3.SQLITE_SELECT
            or (code == sqlite3.SQLITE_READ and schema in (None, "main") and first in database.TABLES)
            or (code == sqlite3.SQLITE_FUNCTION and second in READ_FUNCTIONS)
            or (not source and code == decision.operation and schema == "main" and first == decision.table)
        ):
            return sqlite3.SQLITE_OK
        denied = True
        return sqlite3.SQLITE_DENY

    deadline = monotonic() + 0.2
    db.set_progress_handler(lambda: monotonic() > deadline, 1000)
    is_drop = decision.operation == sqlite3.SQLITE_DROP_TABLE
    if not is_drop:
        db.set_authorizer(authorize)
    before = db.total_changes
    try:
        cursor = db.execute(decision.sql)
        if cursor.description:
            rows = []
            size = 0
            for row in cursor.fetchmany(101):
                value = {
                    key: str(item) if isinstance(item, float) and not math.isfinite(item) else item
                    for key, item in dict(row).items()
                }
                encoded = json.dumps(value, default=str)
                size += len(encoded)
                if len(rows) == 100 or size > 65536:
                    return {"ok": True, "rows": rows, "truncated": True}
                rows.append(json.loads(encoded))
            return {"ok": True, "rows": rows, "truncated": False}
        return {
            "ok": True,
            "rows_affected": db.total_changes - before if not is_drop else None,
        }
    except sqlite3.Error as error:
        if denied:
            return {"ok": False, "error": "Unsupported SQL table, function or operation", "blocked": True}
        return {"ok": False, "error": str(error)}
    finally:
        db.set_authorizer(None)
        db.set_progress_handler(None, 0)


def preview(db: sqlite3.Connection, decision: Decision) -> tuple[dict, bool]:
    before = database.stats(db)
    with closing(database.empty_database()) as copy:
        db.backup(copy)
        result = run_sql(copy, decision)
        after = database.stats(copy) if result["ok"] else before
    diffs = {
        table: {"before": count, "after": after[table], "rows_lost": max(0, count - (after[table] or 0)),
                "rows_added": max(0, (after[table] or 0) - count)}
        for table, count in before.items() if count != after[table]
    }
    affected = result.get("rows_affected") or 0
    if result["ok"] and decision.operation == sqlite3.SQLITE_DROP_TABLE:
        affected = before[decision.table] or 0
    return {
        "scope": "disposable SQLite fixtures", "would_succeed": result["ok"],
        "table": decision.table, "rows_affected": affected,
        "table_diffs": diffs, "error": result.get("error"),
    }, result.get("blocked", False)


def decide_and_route(demo: Demo, kind: str, action: str) -> dict:
    action = action.strip(" \t")
    if not action or len(action) > MAX_ACTION or "\x00" in action:
        decision = blocked("Empty, oversized or invalid proposal")
    elif kind == "sql":
        decision = classify_sql(action)
    elif kind == "shell":
        decision = classify_shell(demo.db, action)
    else:
        decision = blocked("Unknown action kind")
    impact = decision.impact
    if decision.operation is not None:
        impact, denied = preview(demo.db, decision)
        if denied:
            decision = blocked(impact["error"])
    payload = {"classification": decision.classification, "reason": decision.reason, "blast_radius": impact}

    if decision.classification == "blocked":
        status = "blocked"
    elif decision.classification == "destructive" and len(demo.pending) >= 25:
        status = "blocked"
        payload["reason"] = "Pending queue is full"
    elif decision.classification == "destructive":
        pid = demo.next_id
        demo.next_id += 1
        demo.pending[pid] = {
            "id": pid, "created_at": database.timestamp(), "kind": kind, "action": action,
            "classification": decision.classification, "blast_radius": impact, "revision": demo.revision,
        }
        payload["pending_id"] = pid
        status = "pending-approval"
    else:
        if impact.get("would_succeed") is False:
            result = {"ok": False, "error": impact["error"]}
        else:
            result = execute(demo, decision)
        if result.pop("blocked", False):
            payload.update(classification="blocked", reason=result["error"])
            status = "blocked"
        elif not result["ok"]:
            status = "failed"
        else:
            status = {"safe": "executed", "reversible": "executed-reversible"}[decision.classification]
            if kind == "shell":
                status = "simulated"
        payload["result"] = result
    demo.log(kind, action[:MAX_ACTION], payload["classification"], status, impact)
    return {**payload, "status": status}


def execute(demo: Demo, decision: Decision) -> dict:
    if decision.classification not in {"safe", "reversible"}:
        raise ValueError("Only safe or scoped fixture operations can execute")
    if decision.sql is None:
        return {"ok": True, "simulated": True, "message": "Shell command recorded as a simulation."}
    result = run_sql(demo.db, decision)
    if result["ok"] and decision.operation is not None:
        demo.revision += 1
    return result


def pending(demo: Demo) -> list[dict]:
    return [
        {key: value for key, value in row.items() if key != "revision"}
        for row in reversed(demo.pending.values())
    ]


def resolve(demo: Demo, pid: int, approve: bool) -> dict | None:
    row = demo.pending.pop(pid, None)
    if row is None:
        return None
    if not approve:
        status, result = "rejected", {"ok": True, "message": "Action blocked."}
    elif row["revision"] != demo.revision:
        status, result = "stale", {"ok": False, "message": "Fixtures changed. Analyze the action again."}
    else:
        status = "approved-simulated"
        result = {
            "ok": True, "simulated": True, "blast_radius": row["blast_radius"],
            "message": "Approval recorded. The simulation left all fixtures intact.",
        }
    demo.log(row["kind"], row["action"], row["classification"], status, row["blast_radius"])
    return {**result, "status": status}
