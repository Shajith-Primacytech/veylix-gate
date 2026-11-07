"""Veylix Gate web app — FastAPI + Jinja approval UI."""
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import gate
import agent

app = FastAPI(title="Veylix Gate", version="0.1.0")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def _startup() -> None:
    database.init_db()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stats": database.stats(),
            "pending": database.pending(),
            "audit": database.recent_audit(25),
        },
    )


@app.post("/action")
def submit_action(kind: str = Form(...), action: str = Form(...)):
    return JSONResponse(gate.decide_and_route(kind, action))


@app.post("/agent")
def run_agent(task: str = Form(...)):
    return JSONResponse(agent.ask_agent(task))


@app.post("/approve/{pid}")
def approve(pid: int):
    gate.approve(pid)
    return RedirectResponse("/", status_code=303)


@app.post("/reject/{pid}")
def reject(pid: int):
    gate.reject(pid)
    return RedirectResponse("/", status_code=303)


@app.get("/api/pending")
def api_pending():
    return {"pending": database.pending()}


@app.get("/api/audit")
def api_audit():
    return {"audit": database.recent_audit(100)}


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "veylix-gate"}
