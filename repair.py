"""One approval-gated, in-memory sample workload per demo session."""
import secrets
import sqlite3
from time import monotonic

import database

MODE = "sqlite-fixture"
MAX_JOB_ID = 20
APPROVAL_TTL = 5 * 60
WORKLOAD = "SELECT id FROM repair_jobs WHERE id = ? AND enabled = 1"
UPDATE = "UPDATE repair_jobs SET enabled = 1 WHERE id = ? AND enabled = 0"
TERMINAL = {"rejected", "verified", "stale", "repair-failed"}


class Run:
    def __init__(self, job_id: int, actor: str):
        self.id = secrets.token_hex(16)
        self.actor = actor
        self.job_id = job_id
        self.plan = None
        self.approval_deadline = None
        self.events = []
        self.db = database.empty_database()
        try:
            with self.db:
                self.db.execute("BEGIN")
                self.db.execute("""CREATE TABLE repair_jobs (
                    id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                )""")
                self.db.executemany("INSERT INTO repair_jobs VALUES (?, ?)", (
                    (i, int(i != job_id)) for i in range(1, MAX_JOB_ID + 1)
                ))
                self.before = self.measure()
                if self.before["status"] != "failed":
                    raise sqlite3.IntegrityError("Sample workload did not fail")
        except sqlite3.Error:
            self.db.close()
            raise
        self.record("failure-observed", {"workload": WORKLOAD, "parameters": [job_id], "result": self.before})

    def measure(self) -> dict:
        found = self.db.execute(WORKLOAD, (self.job_id,)).fetchone()
        return {"status": "passed" if found else "failed", "rows_returned": int(found is not None)}

    def record(self, status: str, evidence: dict):
        self.status = status
        self.events.append({
            "event_id": secrets.token_hex(16), "ts": database.timestamp(), "mode": MODE,
            "run_id": self.id, "actor": self.actor, "status": status, "evidence": evidence,
        })

    def job_is_disabled(self) -> bool:
        row = self.db.execute("SELECT enabled FROM repair_jobs WHERE id = ?", (self.job_id,)).fetchone()
        return row is not None and row["enabled"] == 0

    def diagnose(self):
        if not self.job_is_disabled():
            self.record("stale", {"reason": "The disabled sample job could not be confirmed."})
            return
        self.plan = {
            "diagnosis": "The sample job is disabled, so the workload returns zero rows.",
            "sql": UPDATE, "parameters": [self.job_id], "expected_rows_updated": 1,
            "expected_after": {"status": "passed", "rows_returned": 1},
            "approval_window_seconds": APPROVAL_TTL,
        }
        self.approval_deadline = monotonic() + APPROVAL_TTL
        self.record("awaiting-approval", {"plan": self.plan})

    def decide(self, approve: bool):
        if monotonic() >= self.approval_deadline:
            self.record("stale", {"reason": "The approval window expired. Create a fresh workload."})
            return
        if not approve:
            self.record("rejected", {"rows_updated": 0})
            return
        self.record("approved", {"plan": self.plan})
        phase = "before-update"
        rows_updated = None
        after = None
        try:
            with self.db:
                self.db.execute("BEGIN")
                if not self.job_is_disabled():
                    self.record("stale", {"reason": "The workload no longer matches the reviewed failure."})
                    return
                phase = "update"
                rows_updated = self.db.execute(UPDATE, (self.job_id,)).rowcount
                if rows_updated != self.plan["expected_rows_updated"]:
                    raise sqlite3.IntegrityError("Unexpected repair scope")
                phase = "verification"
                after = self.measure()
                if after != self.plan["expected_after"]:
                    raise sqlite3.IntegrityError("Workload verification failed")
                phase = "commit"
        except sqlite3.Error:
            self.record("repair-failed", {
                "phase": phase, "attempted_rows_updated": rows_updated,
                "before": self.before, "after": after, "committed": False,
            })
            return
        self.record("verified", {
            "rows_updated": rows_updated, "before": self.before, "after": after, "committed": True,
        })

    def summary(self) -> dict:
        return {
            "mode": MODE, "run_id": self.id, "job_id": self.job_id, "status": self.status,
            "before": self.before, "plan": self.plan, "audit": self.events,
        }
