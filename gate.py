"""Blast-radius gate — classify shell + SQL, dry-run destructive SQL on SQLite."""
import json
import re
import shlex
import sqlite3
from dataclasses import dataclass, asdict

import sqlglot
from sqlglot import exp

import database

SAFE = "safe"
REVERSIBLE = "reversible"
DESTRUCTIVE = "destructive"

DESTRUCTIVE_SHELL_PATTERNS = [
    r"^rm\s+-rf?\b",
    r"^rm\s+.*-rf\b",
    r"^dd\b",
    r"^mkfs",
    r":\(\)\s*{",
    r">\s*/dev/sd",
    r"chmod\s+-R\s+777",
]
DESTRUCTIVE_CLI_VERBS = {
    ("vultr", "volume", "delete"),
    ("vultr", "instance", "delete"),
    ("aws", "s3", "rb"),
    ("aws", "ec2", "terminate-instances"),
}
SAFE_SHELL_PREFIXES = {"ls", "cat", "pwd", "whoami", "echo", "git", "head", "tail", "grep", "wc", "find", "env", "printenv"}


@dataclass
class Decision:
    classification: str
    reason: str
    blast_radius: dict


def classify_shell(cmd: str) -> Decision:
    low = cmd.strip()
    if not low:
        return Decision(SAFE, "empty command", {})
    for pat in DESTRUCTIVE_SHELL_PATTERNS:
        if re.search(pat, low):
            return Decision(DESTRUCTIVE, f"matches destructive pattern: {pat}", {"command": low})
    try:
        tokens = shlex.split(low)
    except ValueError:
        return Decision(DESTRUCTIVE, "unparseable shell command", {"command": low})
    if not tokens:
        return Decision(SAFE, "empty tokens", {})
    head3 = tuple(tokens[:3])
    if head3 in DESTRUCTIVE_CLI_VERBS:
        br = {"command": low, "target": tokens[3] if len(tokens) > 3 else "unknown"}
        if tokens[0] == "vultr" and tokens[1] == "volume":
            br.update(_lookup_volume(tokens[-1]))
        return Decision(DESTRUCTIVE, "destructive cloud CLI verb", br)
    if tokens[0] in SAFE_SHELL_PREFIXES:
        return Decision(SAFE, "read-only shell command", {"command": low})
    return Decision(REVERSIBLE, "unclassified shell — allowed with snapshot", {"command": low})


def _lookup_volume(vid: str) -> dict:
    con = sqlite3.connect(database.DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM cloud_volumes WHERE id=?", (vid,))
    row = cur.fetchone()
    con.close()
    if row is None:
        return {"volume": vid, "note": "volume not found (would be no-op)"}
    return {
        "volume_id": row["id"],
        "volume_name": row["name"],
        "size_gb": row["size_gb"],
        "attached_to": row["attached_to"],
        "backups_also_deleted": bool(row["has_backups"]),
    }


def classify_sql(sql: str) -> Decision:
    try:
        parsed = sqlglot.parse(sql, dialect="sqlite")
    except Exception as e:
        return Decision(DESTRUCTIVE, f"unparseable SQL: {e}", {"sql": sql})
    if not parsed:
        return Decision(SAFE, "empty SQL", {})

    worst = SAFE
    reasons: list[str] = []
    blast: dict = {"sql": sql}

    for stmt in parsed:
        if stmt is None:
            continue
        if isinstance(stmt, exp.Select):
            reasons.append("SELECT")
            continue
        if isinstance(stmt, exp.Drop):
            worst = DESTRUCTIVE
            reasons.append(f"DROP {stmt.args.get('kind') or ''}".strip())
        elif isinstance(stmt, exp.TruncateTable):
            worst = DESTRUCTIVE
            reasons.append("TRUNCATE")
        elif isinstance(stmt, exp.Delete):
            if stmt.args.get("where") is None:
                worst = DESTRUCTIVE
                reasons.append("DELETE without WHERE")
            else:
                if worst != DESTRUCTIVE:
                    worst = REVERSIBLE
                reasons.append("DELETE with WHERE")
        elif isinstance(stmt, exp.Update):
            if stmt.args.get("where") is None:
                worst = DESTRUCTIVE
                reasons.append("UPDATE without WHERE")
            else:
                if worst != DESTRUCTIVE:
                    worst = REVERSIBLE
                reasons.append("UPDATE with WHERE")
        elif isinstance(stmt, exp.Insert):
            if worst != DESTRUCTIVE:
                worst = REVERSIBLE
            reasons.append("INSERT")
        elif isinstance(stmt, exp.AlterTable):
            worst = DESTRUCTIVE
            reasons.append("ALTER TABLE")
        else:
            if worst == SAFE:
                worst = REVERSIBLE
            reasons.append(stmt.key.upper())

    if worst == DESTRUCTIVE:
        blast.update(_dry_run_sql(sql))
    return Decision(worst, "; ".join(reasons), blast)


def _dry_run_sql(sql: str) -> dict:
    """Open a transaction, execute, count what changed, roll back."""
    before = database.stats()
    affected: dict = {}
    error: str | None = None
    con = sqlite3.connect(database.DB_PATH)
    try:
        con.execute("BEGIN")
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            try:
                cur = con.execute(stmt)
                affected[stmt[:80]] = cur.rowcount
            except sqlite3.Error as e:
                error = str(e)
                break
        after = {}
        for t in ("customers", "orders", "cloud_volumes"):
            try:
                after[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                after[t] = None
        con.execute("ROLLBACK")
    finally:
        con.close()

    diffs = {}
    for t, n_before in before.items():
        n_after = after.get(t)
        if n_after is None:
            diffs[t] = {"before": n_before, "after": "table missing", "lost": n_before}
        elif n_after != n_before:
            diffs[t] = {"before": n_before, "after": n_after, "lost": n_before - n_after}
    return {"dry_run": {"affected_statements": affected, "table_diffs": diffs, "error": error}}


def classify(kind: str, action: str) -> Decision:
    if kind == "sql":
        return classify_sql(action)
    if kind == "shell":
        return classify_shell(action)
    return Decision(DESTRUCTIVE, f"unknown action kind: {kind}", {"action": action})


def execute_shell(cmd: str) -> dict:
    """Stub executor — the demo doesn't need to really run shell."""
    database.log_audit(cmd, "shell", "", "executed")
    return {"ok": True, "stdout": f"[simulated] ran: {cmd}", "note": "demo stub"}


def execute_sql(sql: str) -> dict:
    con = sqlite3.connect(database.DB_PATH)
    results: list = []
    try:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur = con.execute(stmt)
            if cur.description:
                rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()[:100]]
                results.append({"sql": stmt, "rows": rows})
            else:
                results.append({"sql": stmt, "rowcount": cur.rowcount})
        con.commit()
    except sqlite3.Error as e:
        con.rollback()
        return {"ok": False, "error": str(e), "results": results}
    finally:
        con.close()
    database.log_audit(sql, "sql", "", "executed")
    return {"ok": True, "results": results}


def decide_and_route(kind: str, action: str) -> dict:
    d = classify(kind, action)
    payload = {"classification": d.classification, "reason": d.reason, "blast_radius": d.blast_radius}
    if d.classification == SAFE:
        exec_fn = execute_sql if kind == "sql" else execute_shell
        result = exec_fn(action)
        payload["status"] = "executed"
        payload["result"] = result
        return payload
    if d.classification == REVERSIBLE:
        exec_fn = execute_sql if kind == "sql" else execute_shell
        result = exec_fn(action)
        payload["status"] = "executed-reversible"
        payload["result"] = result
        return payload
    pid = database.enqueue(kind, action, d.classification, json.dumps(d.blast_radius))
    database.log_audit(action, d.classification, json.dumps(d.blast_radius), "blocked-pending-approval")
    payload["status"] = "pending-approval"
    payload["pending_id"] = pid
    return payload


def approve(pid: int) -> dict:
    row = database.resolve(pid, "approved")
    if row is None:
        return {"ok": False, "error": "pending action not found"}
    exec_fn = execute_sql if row["kind"] == "sql" else execute_shell
    result = exec_fn(row["action"])
    database.log_audit(row["action"], row["classification"], row["blast_radius"], "approved-executed")
    return {"ok": True, "result": result}


def reject(pid: int, reason: str = "human rejected") -> dict:
    row = database.resolve(pid, "rejected")
    if row is None:
        return {"ok": False, "error": "pending action not found"}
    database.log_audit(row["action"], row["classification"], row["blast_radius"], f"rejected: {reason}")
    return {"ok": True, "message": "action blocked"}
