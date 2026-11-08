"""Approval state for RepairLab. This module never opens a database."""
import asyncio
import json
import os
import re
import secrets
from time import monotonic

from repair import APPROVAL_TTL, UPDATE, WORKLOAD
from database import timestamp

MODE = "docker-sqlite-fixture"
FAILED = {"status": "failed", "rows_returned": 0}
PASSED = {"status": "passed", "rows_returned": 1}
HASH = r"[a-f0-9]{64}"
BROKER = "/run/veylix-sandbox/broker.sock"
_busy = False


def enabled():
    return os.environ.get("VEYLIX_ISOLATED") == "1"


async def task(op, job, baseline=None):
    global _busy
    if _busy:
        return None, [{"event": "broker-busy"}]
    _busy = True
    try:
        return await broker_task(op, job, baseline)
    finally:
        _busy = False


async def broker_task(op, job, baseline):
    data = {"op": op, "job": job}
    if baseline is not None:
        data["fixture_sha256"] = baseline
    try:
        async with asyncio.timeout(26):
            reader, writer = await asyncio.open_unix_connection(BROKER, limit=16384)
            try:
                writer.write(json.dumps(data).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
                events = response["events"]
                if not isinstance(events, list):
                    raise ValueError()
                return response["result"] if response["ok"] is True else None, events
            finally:
                writer.close()
    except (OSError, ValueError, KeyError, TypeError, RecursionError, TimeoutError):
        return None, [{"event": "broker-unavailable"}]


class Run:
    def __init__(self, job_id, actor):
        self.id = secrets.token_hex(16)
        self.actor = actor
        self.job_id = job_id
        self.plan = self.before = self.baseline = self.approval_deadline = None
        self.events = []
        self.status = "starting"

    def close(self):
        pass

    def record(self, status, evidence):
        self.status = status
        self.events.append({
            "event_id": secrets.token_hex(16), "ts": timestamp(), "mode": MODE,
            "run_id": self.id, "actor": self.actor, "status": status, "evidence": evidence,
        })

    async def observe(self):
        result, events = await task("observe", self.job_id)
        if not isinstance(result, dict) or result.get("before") != FAILED or not valid_hash(
            result.get("fixture_sha256")
        ):
            self.record("repair-failed", {"phase": "observe", "sandbox": events})
            return
        self.before, self.baseline = result["before"], result["fixture_sha256"]
        self.record("failure-observed", {"workload": WORKLOAD, "parameters": [self.job_id],
                    "result": self.before, "fixture_sha256": self.baseline, "sandbox": events})

    def diagnose(self, evidence):
        self.plan = {
            "operation": "enable_sample_job",
            "diagnosis": "The sample job is disabled, so the workload returns zero rows.",
            "sql": UPDATE, "parameters": [self.job_id], "expected_rows_updated": 1,
            "expected_after": PASSED,
            "expected_after": PASSED, "approval_window_seconds": APPROVAL_TTL,
        }
        self.approval_deadline = monotonic() + APPROVAL_TTL
        self.record("awaiting-approval", {"plan": self.plan, "inference": evidence})

    async def decide(self, approve):
        if monotonic() >= self.approval_deadline:
            self.record("stale", {"reason": "The approval window expired. Create a fresh workload."})
            return
        if not approve:
            self.record("rejected", {"rows_updated": 0})
            return
        self.record("approved", {"plan": self.plan})
        result, events = await task("apply", self.job_id, self.baseline)
        evidence = {"sandbox": events}
        if (
            not isinstance(result, dict) or result.get("before") != FAILED
            or result.get("fixture_sha256") != self.baseline or result.get("after") != PASSED
            or type(result.get("rows_updated")) is not int or result["rows_updated"] != 1
            or result.get("committed") is not True or not valid_hash(result.get("repaired_sha256"))
            or result["repaired_sha256"] == self.baseline
        ):
            self.record("repair-failed", {"phase": "apply", **evidence})
            return
        self.record("verified", {**result, **evidence})

    def summary(self):
        return {"mode": MODE, "run_id": self.id, "job_id": self.job_id, "status": self.status,
                "before": self.before, "plan": self.plan, "audit": self.events}


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(HASH, value) is not None
