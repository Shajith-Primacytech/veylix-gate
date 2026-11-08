"""Host-only Docker broker. The API can send only fixture job commands over Unix IPC."""
import argparse
import fcntl
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from task import MEMORY, PIDS, SCRATCH, TMPFS, USER

LABEL = "com.veylix.repairlab.sandbox=challenge1"
PROBES = {"probe-timeout", "probe-network", "probe-limits", "probe-memory", "probe-pids"}
DOCKER_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"}
ENGINE = ("docker", "--host", "unix:///var/run/docker.sock")
HASH = r"[a-f0-9]{64}"


def docker(*args, timeout=3):
    return subprocess.run((*ENGINE, *args), timeout=timeout, env=DOCKER_ENV,
                          text=True, capture_output=True, check=True)


def validate(data):
    if not isinstance(data, dict):
        raise ValueError()
    op, job = data.get("op"), data.get("job")
    if op not in ("observe", "apply") or type(job) is not int or not 1 <= job <= 20:
        raise ValueError()
    if op == "observe" and set(data) == {"op", "job"}:
        return op, str(job)
    baseline = data.get("fixture_sha256")
    if op == "apply" and set(data) == {"op", "job", "fixture_sha256"} and (
        isinstance(baseline, str) and re.fullmatch(HASH, baseline)
    ):
        return op, str(job), baseline
    raise ValueError()


def inspect(target):
    return json.loads(docker("inspect", "--type=container", target).stdout)[0]


def permitted(obj, image):
    h, c = obj["HostConfig"], obj["Config"]
    return (
        obj["Image"] == image and c["User"] == USER and h["NetworkMode"] == "none"
        and h["ReadonlyRootfs"] and h["Tmpfs"] == {SCRATCH: TMPFS}
        and not h["Binds"] and not h.get("Mounts") and not obj["Mounts"] and not c["Volumes"]
        and not h["VolumesFrom"] and not h["Privileged"] and not h["Devices"]
        and not h["DeviceRequests"] and not h.get("DeviceCgroupRules") and not h["PortBindings"] and not h["PublishAllPorts"]
        and not h["CapAdd"] and h["CapDrop"] == ["ALL"]
        and h["SecurityOpt"] in (["no-new-privileges"], ["no-new-privileges:true"])
        and h["Memory"] == h["MemorySwap"] == MEMORY and h["PidsLimit"] == PIDS
        and h["NanoCpus"] == 500_000_000 and h["CgroupnsMode"] == "private"
        and h["PidMode"] == "" and h["IpcMode"] == "none"
        and h["RestartPolicy"]["Name"] in ("", "no")
        and c["Entrypoint"] == ["/usr/local/bin/python", "-IB", "/task/task.py"]
        and all(re.fullmatch(r"(?:PATH|LANG|PYTHON_VERSION|PYTHON_SHA256|GPG_KEY)=[^\r\n]*", item)
                for item in c["Env"])
    )


def execute(image, args):
    name = "veylix-c1-" + uuid4().hex
    events = []
    result = None
    cid = None

    def event(kind, **evidence):
        item = {"event": kind, "ts": datetime.now(timezone.utc).isoformat(),
                "task": args[0], "container_id": cid, **evidence}
        events.append(item)
        print(json.dumps(item), flush=True)

    try:
        event("create-requested", image=image, name=name)
        cid = docker("create", "--pull=never", "--name", name, "--label", LABEL,
                     "--user", USER, "--read-only", "--tmpfs", f"{SCRATCH}:{TMPFS}",
                     "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                     "--memory=64m", "--memory-swap=64m", "--cpus=0.5", "--pids-limit=16",
                     "--cgroupns=private", "--ipc=none", "--log-driver=none",
                     image, *args).stdout.strip()
        obj = inspect(cid)
        event("created", image=obj["Image"])
        if not permitted(obj, image) or obj["Config"]["Cmd"] != list(args):
            raise ValueError("policy")
        event("limits-checked", network="none", root_read_only=True, user=USER, cap_drop=["ALL"],
              no_new_privileges=True, scratch=TMPFS, memory_bytes=MEMORY, memory_swap_bytes=MEMORY,
              cpus=0.5, pids=PIDS, host_timeout_seconds=3 if args[0] == "probe-timeout" else 6)
        event("start-requested")
        proc = subprocess.run((*ENGINE, "start", "--attach", cid), env=DOCKER_ENV,
                              text=True, capture_output=True, timeout=3 if args[0] == "probe-timeout" else 6)
        state = inspect(cid)["State"]
        if state["StartedAt"].startswith("0001-") or state["Running"]:
            raise ValueError("did-not-complete")
        event("started", started_at=state["StartedAt"])
        event("completed", exit_code=state["ExitCode"], oom_killed=state["OOMKilled"],
              finished_at=state["FinishedAt"])
        if state["ExitCode"] == 0 and proc.returncode == 0 and len(proc.stdout) <= 8192:
            output = json.loads(proc.stdout)
            event("runtime-checked", **output["runtime"])
            result = output["result"]
            if args[0] in PROBES:
                event("probe-result", result=result)
    except subprocess.TimeoutExpired as exc:
        if cid and exc.cmd[-2:] == ("--attach", cid):
            try:
                state = inspect(cid)["State"]
                if not state["StartedAt"].startswith("0001-"):
                    event("started", started_at=state["StartedAt"])
            except (subprocess.SubprocessError, ValueError, KeyError, IndexError, OSError):
                pass
            event("timed-out", timeout_seconds=exc.timeout)
        else:
            event("sandbox-failed", reason="Docker operation timed out")
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError, OSError):
        event("sandbox-failed", reason="Docker launch, policy, or worker output check failed")
    finally:
        try:
            # The unique name also handles partial create where the client missed the response.
            subprocess.run((*ENGINE, "rm", "-f", "-v", name), env=DOCKER_ENV,
                           capture_output=True, timeout=3)
            remaining = docker("ps", "-aq", "--filter", f"name=^/{name}$").stdout.strip()
            if remaining:
                raise RuntimeError()
            event("cleanup-confirmed")
        except (subprocess.SubprocessError, RuntimeError, OSError):
            event("cleanup-unconfirmed")
            result = None
    return {"ok": result is not None, "result": result, "events": events}


def handle(conn, image):
    conn.settimeout(2)
    end = time.monotonic() + 2
    raw = bytearray()
    try:
        while len(raw) <= 1024 and time.monotonic() < end:
            conn.settimeout(max(.001, end - time.monotonic()))
            part = conn.recv(1025 - len(raw))
            if not part:
                break
            raw.extend(part)
            if raw.endswith(b"\n"):
                break
            if len(raw) == 1025:
                break
        if len(raw) > 1024 or not raw.endswith(b"\n"):
            raise ValueError()
        args = validate(json.loads(raw))
    except (ValueError, RecursionError, OSError):
        return
    response = execute(image, args)
    try:
        conn.sendall(json.dumps(response).encode() + b"\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--directory", type=Path, default=Path("/run/veylix-sandbox"))
    parser.add_argument("--self-test", action="store_true")
    options = parser.parse_args()
    if not re.fullmatch(r"sha256:" + HASH, options.image):
        parser.error("Use a reviewed, immutable local image ID")
    path = options.directory
    path.mkdir(mode=0o755, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
        parser.error("Socket directory must be owned by the broker and not writable by other users")
    with (path / "broker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = json.loads(docker("info", "--format", "{{json .}}", timeout=10).stdout)
        if info["OSType"] != "linux" or info["CgroupVersion"] != "2" or not all(
            info[k] for k in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
        ):
            parser.error("Docker Linux cgroup v2 with memory, swap, CPU and PID limits is required")
        if docker("image", "inspect", "--format", "{{.Id}}", options.image).stdout.strip() != options.image:
            parser.error("Worker image not available")
        for cid in docker("ps", "-aq", "--filter", f"label={LABEL}",
                          "--filter", "name=^/veylix-c1-").stdout.split():
            docker("rm", "-f", "-v", cid)
            print(json.dumps({"event": "orphan-removed", "container_id": cid}), flush=True)
        if options.self_test:
            outcomes = {}
            for probe in sorted(PROBES):
                run = execute(options.image, (probe,))
                events = {e["event"]: e for e in run["events"]}
                passed = run["ok"] if probe in {"probe-network", "probe-limits", "probe-pids"} else (
                    "timed-out" in events and "started" in events if probe == "probe-timeout"
                    else events.get("completed", {}).get("oom_killed") is True
                )
                outcomes[probe] = passed and "cleanup-confirmed" in events
            print(json.dumps({"self_test": outcomes}), flush=True)
            return 0 if all(outcomes.values()) else 1
        address = path / "broker.sock"
        address.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(address))
            os.chown(address, -1, 10001)
            address.chmod(0o660)
            listener.listen(2)
            while True:
                conn, _ = listener.accept()
                with conn:
                    if not handle_peer(conn):
                        continue
                    handle(conn, options.image)


def handle_peer(conn):
    # Linux SO_PEERCRED authenticates the sole allowed container UID.
    import struct
    _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid == 10001


if __name__ == "__main__":
    raise SystemExit(main())
