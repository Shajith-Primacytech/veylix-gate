"""Authenticated demo API and the standalone Jinja operator UI."""
import asyncio
import json
import os
import re
import secrets
import sqlite3
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import agent
import database
import gate
import isolated
import repair

SESSION_TTL = 30 * 60
MAX_SESSIONS = 64
MAX_BODY = 8192
SECURE_COOKIE = "__Host-veylix-demo"
LOCAL_COOKIE = "veylix-local-demo"
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


@dataclass
class Session:
    expires: float
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    demo: gate.Demo | None = field(default_factory=lambda: None if isolated.enabled() else gate.Demo())
    requests: deque = field(default_factory=deque)
    inference: deque = field(default_factory=deque)
    repair_actor: str = field(default_factory=lambda: secrets.token_hex(16))
    repair_run: repair.Run | isolated.Run | None = None


class Sessions:
    def __init__(self):
        self.active: dict[str, Session] = {}
        self.login_requests = deque()
        self.api_requests = deque()
        self.inference_requests = deque()
        self.inflight_inference = 0

    def discard(self, sid: str):
        session = self.active.pop(sid)
        if session.demo is not None:
            session.demo.db.close()
        if session.repair_run is not None:
            session.repair_run.close()

    def expire(self):
        for sid, session in list(self.active.items()):
            if session.expires <= monotonic():
                self.discard(sid)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sessions = Sessions()

    async def cleanup():
        while True:
            await asyncio.sleep(15)
            app.state.sessions.expire()

    worker = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        for sid in list(app.state.sessions.active):
            app.state.sessions.discard(sid)


app = FastAPI(title="Veylix Gate", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def matches(received: str, expected: str) -> bool:
    return secrets.compare_digest(received.encode(), expected.encode())


def limit(hits: deque, count: int, period: int = 60):
    now = monotonic()
    while hits and hits[0] <= now - period:
        hits.popleft()
    if len(hits) >= count:
        raise HTTPException(429, "Demo rate limit exceeded", headers={"Retry-After": str(period)})
    hits.append(now)


def local_request(request: Request) -> bool:
    local = os.environ.get("GATE_LOCAL_ORIGIN")
    return bool(
        local and re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", local)
        and request.client and request.client.host == "127.0.0.1"
        and request.headers.get("host") == urlsplit(local).netloc
        and request.url.scheme == "http"
    )


JUDGE_PATHS = {
    "/", "/judge.js", "/healthz", "/session", "/logout", "/api/audit", "/api/repair",
    "/api/repair/start", "/api/repair/diagnose", "/api/repair/decide",
}


def judge_origin():
    return os.environ.get("VEYLIX_JUDGE_ORIGIN", "")


def judge_request(request):
    origin = judge_origin()
    return bool(re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]{1,5})?", origin)
                and request.headers.get("host") == urlsplit(origin).netloc)


@app.middleware("http")
async def guard(request: Request, call_next):
    proxy_token = os.environ.get("GATE_PROXY_TOKEN", "")
    local = local_request(request)
    proxied = len(proxy_token) >= 32 and matches(request.headers.get("x-gate-proxy-token", ""), proxy_token)
    request.state.proxied = proxied
    if isolated.enabled():
        request.state.local = False
        if request.url.path == "/healthz" or (judge_request(request) and request.url.path in JUDGE_PATHS):
            response = await call_next(request)
        else:
            response = JSONResponse({"detail": "Route unavailable"}, status_code=403)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Strict-Transport-Security"] = "max-age=86400"
    elif request.url.path == "/healthz" or local or proxied:
        request.state.local = local
        response = await call_next(request)
    else:
        response = JSONResponse({"detail": "Gateway authentication required"}, status_code=403)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin" if local else "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def require_origin(request: Request):
    configured = (
        [os.environ.get("GATE_LOCAL_ORIGIN")] if request.state.local
        else os.environ.get("GATE_ALLOWED_ORIGINS", "").split(",")
    )
    if isolated.enabled():
        configured = [judge_origin(), *configured] if request.state.proxied else [judge_origin()]
    origin = request.headers.get("origin")
    if not origin or origin not in configured or (not request.state.local and not origin.startswith("https://")):
        raise HTTPException(403, "Origin not allowed")


def cookie_name(request: Request) -> str:
    return LOCAL_COOKIE if request.state.local else SECURE_COOKIE


def require_session(request: Request, *, charge: bool = True) -> Session:
    sessions = request.app.state.sessions
    sessions.expire()
    sid = request.cookies.get(cookie_name(request), "")
    session = sessions.active.get(sid)
    if session is None:
        raise HTTPException(401, "Start an authenticated demo session")
    if charge:
        limit(sessions.api_requests, 240)
        limit(session.requests, 45)
    return session


async def fields(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/json", "application/x-www-form-urlencoded"}:
        raise HTTPException(415, "Send JSON or URL-encoded form fields")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Request body is too large")
        body.extend(chunk)
    try:
        if content_type == "application/json":
            data = json.loads(body)
        else:
            data = dict(parse_qsl(body.decode(), max_num_fields=8, keep_blank_values=True))
    except (ValueError, RecursionError):
        raise HTTPException(400, "Invalid request body") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected an object")
    return data


def text_field(data: dict, name: str, max_length: int) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise HTTPException(400, f"Invalid {name}")
    try:
        value.encode()
    except UnicodeError:
        raise HTTPException(400, f"Invalid {name}") from None
    return value


async def mutation(request: Request, *, form_token: bool = False) -> tuple[Session, dict]:
    require_origin(request)
    session = require_session(request)
    data = await fields(request)
    csrf = request.headers.get("x-csrf-token")
    if csrf is None and form_token and request.state.local:
        csrf = text_field(data, "csrf", 64)
    if not csrf or not matches(csrf, session.csrf):
        raise HTTPException(403, "CSRF token required")
    if require_session(request, charge=False) is not session:
        raise HTTPException(401, "Session expired")
    return session, data


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if isolated.enabled():
        return FileResponse(Path(__file__).with_name("templates") / "judge.html")
    if not request.state.local:
        raise HTTPException(404)
    try:
        session = require_session(request)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8"><title>Veylix Gate</title>'
            '<h1>Veylix Gate operator demo</h1><form method="post" action="/session">'
            '<label>Demo access code <input name="access_code" type="password" required></label>'
            '<button>Start demo session</button></form></html>'
        )
    return templates.TemplateResponse(request=request, name="index.html", context={
        "stats": database.stats(session.demo.db), "pending": gate.pending(session.demo),
        "audit": list(session.demo.audit)[:25], "csrf": session.csrf,
    })


@app.get("/judge.js")
async def judge_script():
    if not isolated.enabled():
        raise HTTPException(404)
    return FileResponse(Path(__file__).with_name("templates") / "judge.js", media_type="text/javascript")


@app.post("/session")
async def login(request: Request):
    require_origin(request)
    sessions = request.app.state.sessions
    limit(sessions.login_requests, 20)
    data = await fields(request)
    code = os.environ.get("GATE_DEMO_ACCESS_CODE", "")
    if len(code) < 32:
        raise HTTPException(503, "Demo access is not configured")
    if not matches(text_field(data, "access_code", 256), code):
        raise HTTPException(401, "Incorrect demo access code")
    sessions.expire()
    previous = request.cookies.get(cookie_name(request))
    if previous in sessions.active:
        sessions.discard(previous)
    if len(sessions.active) >= MAX_SESSIONS:
        raise HTTPException(503, "Demo session capacity reached")
    sid = secrets.token_urlsafe(32)
    session = sessions.active[sid] = Session(monotonic() + SESSION_TTL)
    response = (
        RedirectResponse("/", status_code=303)
        if request.state.local and request.headers.get("content-type") == "application/x-www-form-urlencoded"
        else JSONResponse({"csrf": session.csrf}, status_code=201)
    )
    response.set_cookie(cookie_name(request), sid, httponly=True, secure=not request.state.local,
                        samesite="strict", max_age=SESSION_TTL, path="/")
    return response


@app.get("/session")
async def current_session(request: Request):
    return {"csrf": require_session(request).csrf}


@app.post("/logout")
async def logout(request: Request):
    await mutation(request)
    request.app.state.sessions.discard(request.cookies[cookie_name(request)])
    response = JSONResponse({"ok": True})
    response.delete_cookie(cookie_name(request), path="/", secure=not request.state.local,
                           httponly=True, samesite="strict")
    return response


@app.post("/action")
async def submit_action(request: Request):
    session, data = await mutation(request, form_token=True)
    kind = text_field(data, "kind", 16)
    action = text_field(data, "action", gate.MAX_ACTION)
    return gate.decide_and_route(session.demo, kind, action)


@app.post("/agent")
async def run_agent(request: Request):
    session, data = await mutation(request)
    task = text_field(data, "task", 1000)
    sessions = request.app.state.sessions
    limit(session.inference, 3)
    limit(sessions.inference_requests, 12)
    if sessions.inflight_inference >= 3:
        raise HTTPException(429, "Inference is busy", headers={"Retry-After": "5"})
    sessions.inflight_inference += 1
    try:
        proposal, evidence = await asyncio.wait_for(agent.propose(task), timeout=12)
    except ValueError as error:
        raise HTTPException(502, str(error)) from None
    except asyncio.TimeoutError:
        raise HTTPException(504, "Vultr inference timed out") from None
    finally:
        sessions.inflight_inference -= 1
    if require_session(request, charge=False) is not session:
        raise HTTPException(401, "Session expired")
    result = gate.decide_and_route(session.demo, proposal["kind"], proposal["action"])
    session.demo.log("inference", evidence["completion_id"], "inference", "provider-response", evidence)
    return {"proposed": proposal, "inference": evidence, "gate": result}


async def decide(request: Request, pid: int, approve: bool):
    session, _ = await mutation(request, form_token=True)
    result = gate.resolve(session.demo, pid, approve)
    if result is None:
        raise HTTPException(404, "Pending action not found")
    if request.state.local and request.headers.get("content-type") == "application/x-www-form-urlencoded":
        return RedirectResponse("/", status_code=303)
    return JSONResponse(result, status_code=409 if result["status"] == "stale" else 200)


@app.post("/approve/{pid}")
async def approve(request: Request, pid: int):
    return await decide(request, pid, True)


@app.post("/reject/{pid}")
async def reject(request: Request, pid: int):
    return await decide(request, pid, False)


@app.get("/api/pending")
async def api_pending(request: Request):
    return {"pending": gate.pending(require_session(request).demo)}


@app.get("/api/audit")
async def api_audit(request: Request):
    session = require_session(request)
    if isolated.enabled():
        return {"audit": session.repair_run.events if session.repair_run else []}
    return {"audit": list(session.demo.audit)}


@app.get("/api/repair")
async def repair_status(request: Request):
    run = require_session(request).repair_run
    return {"run": run.summary() if run else None}


@app.post("/api/repair/start")
async def repair_start(request: Request):
    session, data = await mutation(request)
    job_id = data.get("job_id")
    if data.keys() != {"job_id"} or type(job_id) is not int or not 1 <= job_id <= repair.MAX_JOB_ID:
        raise HTTPException(400, "Choose a sample job from 1 through 20")
    previous = session.repair_run
    if previous is not None and previous.status not in repair.TERMINAL:
        raise HTTPException(409, "Finish reviewing the current workload first")
    if isolated.enabled():
        run = isolated.Run(job_id, session.repair_actor)
        session.repair_run = run
        await run.observe()
        if require_session(request, charge=False) is not session:
            raise HTTPException(401, "Session expired")
        return {"run": run.summary()}
    try:
        run = repair.Run(job_id, session.repair_actor)
    except sqlite3.Error:
        raise HTTPException(503, "The sample failure could not be verified") from None
    session.repair_run = run
    if previous is not None:
        previous.close()
    return {"run": run.summary()}


def reviewable_run(session: Session, data: dict, expected_status: str) -> repair.Run | isolated.Run:
    run_id = data.get("run_id")
    run = session.repair_run
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise HTTPException(400, "Invalid repair run ID")
    if run is None or run.id != run_id:
        raise HTTPException(404, "Repair run not found")
    if run.status != expected_status:
        raise HTTPException(409, "This repair step is no longer available")
    return run


@app.post("/api/repair/diagnose")
async def repair_diagnose(request: Request):
    session, data = await mutation(request)
    if data.keys() != {"run_id"}:
        raise HTTPException(400, "Send only the repair run ID")
    run = reviewable_run(session, data, "failure-observed")
    if isinstance(run, isolated.Run):
        sessions = request.app.state.sessions
        limit(session.inference, 3)
        limit(sessions.inference_requests, 12)
        if sessions.inflight_inference >= 3:
            raise HTTPException(429, "Inference is busy", headers={"Retry-After": "5"})
        sessions.inflight_inference += 1
        run.status = "diagnosing"
        try:
            evidence = await asyncio.wait_for(agent.propose_repair(run.job_id), timeout=12)
        except agent.InferenceTruncated as error:
            run.status = "failure-observed"
            raise HTTPException(502, str(error)) from None
        except (ValueError, TimeoutError):
            run.status = "failure-observed"
            raise HTTPException(502, "Vultr inference could not produce a valid proposal") from None
        finally:
            sessions.inflight_inference -= 1
        if require_session(request, charge=False) is not session:
            raise HTTPException(401, "Session expired")
        run.diagnose(evidence)
        return {"run": run.summary()}
    try:
        run.diagnose()
    except sqlite3.Error:
        run.record("repair-failed", {"phase": "diagnosis", "committed": False})
    return {"run": run.summary()}


@app.post("/api/repair/decide")
async def repair_decide(request: Request):
    session, data = await mutation(request)
    if data.keys() != {"run_id", "decision"} or data.get("decision") not in ("approve", "reject"):
        raise HTTPException(400, "Choose approve or reject for the repair run")
    run = reviewable_run(session, data, "awaiting-approval")
    if isinstance(run, isolated.Run):
        await run.decide(data["decision"] == "approve")
        if require_session(request, charge=False) is not session:
            raise HTTPException(401, "Session expired")
    else:
        run.decide(data["decision"] == "approve")
    return {"run": run.summary()}


@app.get("/healthz")
async def healthz():
    if isolated.enabled():
        return {"ok": True, "service": "veylix-gate", "mode": isolated.MODE,
                "revision": os.environ.get("VEYLIX_REVISION", "unrecorded")}
    return {"ok": True, "service": "veylix-gate"}
