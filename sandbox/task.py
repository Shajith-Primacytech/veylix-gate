"""Trusted task catalog. Runs as PID 1 inside a disposable Linux container."""
import errno
import hashlib
import json
import os
import signal
import socket
import sqlite3
import sys
import time
from pathlib import Path

USER = "10001:10001"
MEMORY = 64 * 1024 * 1024
PIDS = 16
CPU_QUOTA = "50000 100000"
SCRATCH = "/scratch"
TMPFS = "rw,noexec,nosuid,nodev,size=8m,mode=1777"
WORKLOAD = "SELECT id FROM repair_jobs WHERE id = ? AND enabled = 1"
UPDATE = "UPDATE repair_jobs SET enabled = 1 WHERE id = ? AND enabled = 0"
FAILED = {"status": "failed", "rows_returned": 0}
PASSED = {"status": "passed", "rows_returned": 1}


def require(ok):
    if not ok:
        raise RuntimeError("Sandbox policy check failed")


def attest():
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        mounts[fields[4]] = (right.split()[0], set(fields[5].split(",")))
    cgroup = Path("/sys/fs/cgroup")
    observed = {name: (cgroup / name).read_text().strip() for name in
                ("memory.max", "memory.swap.max", "pids.max", "cpu.max")}
    require(observed == {"memory.max": str(MEMORY), "memory.swap.max": "0",
                         "pids.max": str(PIDS), "cpu.max": CPU_QUOTA})
    require(os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 10001)
    require(all(int(status[key].strip(), 16) == 0 for key in
                ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")))
    require(status["NoNewPrivs"].strip() == "1" and status["Seccomp"].strip() == "2")
    require("ro" in mounts["/"][1] and mounts[SCRATCH][0] == "tmpfs" and
            {"rw", "noexec", "nosuid", "nodev"} <= mounts[SCRATCH][1])
    require({name for _, name in socket.if_nameindex()} == {"lo"})
    require(os.statvfs(SCRATCH).f_frsize * os.statvfs(SCRATCH).f_blocks == 8 * 1024 * 1024)
    return {"uid": os.getuid(), "network_interfaces": ["lo"], "root_read_only": True,
            "scratch_bytes": 8 * 1024 * 1024, "capabilities": 0,
            "no_new_privileges": True, "seccomp": True, "cgroups": observed}


def execute(op, job, reviewed_hash):
    def measure():
        return PASSED if db.execute(WORKLOAD, (job,)).fetchone() else FAILED

    def digest():
        return hashlib.sha256(db.serialize()).hexdigest()

    db = sqlite3.connect(f"{SCRATCH}/fixture.sqlite3")
    try:
        with db:
            db.execute("CREATE TABLE repair_jobs (id INTEGER PRIMARY KEY, "
                       "enabled INTEGER NOT NULL CHECK (enabled IN (0,1)))")
            db.executemany("INSERT INTO repair_jobs VALUES (?,?)",
                           ((i, int(i != job)) for i in range(1, 21)))
        before = measure()
        baseline = digest()
        require(before == FAILED)
        if op == "observe":
            return {"before": before, "fixture_sha256": baseline}
        require(op == "apply" and baseline == reviewed_hash)
        with db:
            rows = db.execute(UPDATE, (job,)).rowcount
            require(rows == 1 and measure() == PASSED)
        # Reopen the committed database and rerun the original workload.
        db.close()
        db = sqlite3.connect(f"file:{SCRATCH}/fixture.sqlite3?mode=ro", uri=True)
        after = measure()
        require(after == PASSED)
        return {"before": before, "fixture_sha256": baseline, "after": after,
                "rows_updated": rows, "committed": True, "repaired_sha256": digest()}
    finally:
        db.close()


def deadline(*_):
    os._exit(124)


def main():
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(8)
    proof = attest()
    op = sys.argv[1]
    if op in {"observe", "apply"}:
        result = execute(op, int(sys.argv[2]), sys.argv[3] if op == "apply" else None)
    elif op == "probe-network":
        try:
            with socket.create_connection(("192.0.2.1", 443), timeout=1):
                raise RuntimeError("Network connection unexpectedly succeeded")
        except OSError as exc:
            require(exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH))  # ENETUNREACH / EHOSTUNREACH
            result = {"egress": "denied", "errno": exc.errno}
    elif op == "probe-limits":
        result = {"limits": "enforced"}
    elif op == "probe-pids":
        children = []
        try:
            for _ in range(PIDS):
                try:
                    pid = os.fork()
                except OSError as exc:
                    require(exc.errno == errno.EAGAIN and len(children) == PIDS - 1)
                    break
                if pid == 0:
                    signal.pause()
                    os._exit(0)
                children.append(pid)
            else:
                raise RuntimeError("PID limit did not stop fork")
            result = {"fork_denied": True, "child_count": len(children)}
        finally:
            for pid in children:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
    elif op == "probe-memory":
        # Touch real pages and let the kernel kill this container at memory.max.
        allocations = []
        while len(allocations) < 128:
            allocations.append(bytearray(b"x" * (1024 * 1024)))
        raise RuntimeError("Memory limit did not stop allocation")
    elif op == "probe-timeout":
        time.sleep(30)
        raise RuntimeError("Deadline did not stop worker")
    else:
        raise RuntimeError("Unknown task")
    print(json.dumps({"result": result, "runtime": proof}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(70)
