import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

import agent
import app
import isolated
import repair

ORIGIN = "https://demo.example"
TOKEN = "test-gateway-token-" * 3
CODE = "test-operator-code-" * 3
HEADERS = {"Origin": ORIGIN, "X-Gate-Proxy-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GATE_PROXY_TOKEN", TOKEN)
    monkeypatch.setenv("GATE_DEMO_ACCESS_CODE", CODE)
    monkeypatch.setenv("GATE_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.delenv("GATE_LOCAL_ORIGIN", raising=False)
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("VULTR_INFERENCE_MODEL", raising=False)
    with TestClient(app.app, base_url="https://backend.example", headers=HEADERS) as browser:
        yield browser


def login(client, **kwargs):
    response = client.post("/session", json={"access_code": CODE}, **kwargs)
    assert response.status_code == 201, response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-veylix-demo=")
    assert all(part in cookie for part in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"))
    assert "Domain=" not in cookie
    return {"X-CSRF-Token": response.json()["csrf"]}


def submit(client, auth, action, kind="sql"):
    response = client.post("/action", headers=auth, json={"kind": kind, "action": action})
    assert response.status_code == 200, response.text
    return response.json()


def test_sql_review_and_audit(client):
    auth = login(client)
    action = "SELECT id, plan FROM customers WHERE id=1;"
    assert submit(client, auth, action)["result"]["rows"] == [{"id": 1, "plan": "starter"}]
    update = submit(client, auth, "UPDATE customers SET plan='pro' WHERE id=1;")
    assert update["status"] == "executed-reversible"
    assert update["blast_radius"]["rows_affected"] == update["result"]["rows_affected"] == 1
    drop = submit(client, auth, "DROP TABLE customers;")
    assert drop["status"] == "pending-approval"
    assert drop["blast_radius"]["would_succeed"] is False
    assert "FOREIGN KEY" in drop["blast_radius"]["error"]
    assert drop["blast_radius"]["table_diffs"] == {}
    pid = drop["pending_id"]
    assert client.get("/api/pending").json()["pending"][0]["id"] == pid
    assert client.post(f"/reject/{pid}", headers=auth, json={}).json()["status"] == "rejected"
    assert client.post(f"/approve/{pid}", headers=auth, json={}).status_code == 404
    assert submit(client, auth, action)["result"]["rows"] == [{"id": 1, "plan": "pro"}]
    assert submit(client, auth, "SELECT count(*) AS n FROM orders")["result"]["rows"] == [{"n": 1200}]
    assert submit(client, auth, "SELECT count(*) AS n FROM customers")["result"]["rows"] == [{"n": 250}]
    audit = client.get("/api/audit").json()["audit"]
    assert {item["decision"] for item in audit} >= {"executed", "executed-reversible", "pending-approval", "rejected"}


def test_mock_volume(client):
    auth = login(client)
    report = submit(client, auth, "vultr volume delete vol_prod_db_01", "shell")
    impact = report["blast_radius"]
    assert impact["simulated"] and impact["matched_fixture_volumes"] == 1
    assert impact["size_gb"] == 500 and impact["fixture_has_backups"]
    assert impact["backup_deletion"] == "Not modeled"
    result = client.post(f"/approve/{report['pending_id']}", headers=auth, json={}).json()
    assert result["status"] == "approved-simulated" and result["simulated"]
    assert submit(client, auth, "SELECT count(*) AS n FROM cloud_volumes")["result"]["rows"] == [{"n": 2}]
    missing = submit(client, auth, "vultr volume delete missing", "shell")
    assert missing["blast_radius"]["matched_fixture_volumes"] == 0


def test_scope_and_approval_race(client):
    auth = login(client)
    broad = submit(client, auth, "UPDATE customers SET plan='free' WHERE 1=1")
    assert broad["status"] == "pending-approval" and broad["blast_radius"]["rows_affected"] == 250
    submit(client, auth, "UPDATE customers SET plan='pro' WHERE id=1;")
    assert client.post(f"/approve/{broad['pending_id']}", headers=auth, json={}).status_code == 409
    drop = submit(client, auth, "DROP TABLE orders;")
    assert drop["blast_radius"]["table_diffs"]["orders"]["rows_lost"] == 1200
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(
            lambda _: client.post(f"/approve/{drop['pending_id']}", headers=auth, json={}), range(2)
        ))
    assert sorted(r.status_code for r in replies) == [200, 404]
    success = next(r for r in replies if r.status_code == 200)
    assert success.json()["status"] == "approved-simulated"
    assert submit(client, auth, "SELECT count(*) AS n FROM orders")["result"]["rows"] == [{"n": 1200}]


@pytest.mark.parametrize(("kind", "command"), [
    ("shell", "git reset --hard"),
    ("shell", "ls; rm -rf /"),
    ("shell", "find . -delete"),
    ("shell", "echo $(id)"),
    ("shell", "ls\npwd"),
    ("shell", "vultr volume delete --force vol_prod_db_01"),
    ("shell", "unknown-command"),
    ("sql", "PRAGMA foreign_keys=OFF"),
    ("sql", "ATTACH DATABASE '/tmp/untrusted.db' AS other"),
    ("sql", "SELECT 1; DROP TABLE orders"),
    ("sql", "SELECT load_extension('/tmp/evil')"),
    ("sql", "SELECT * FROM sqlite_schema"),
    ("sql", "UPDATE OR REPLACE customers SET id=2 WHERE id=1"),
    ("sql", "INSERT OR REPLACE INTO customers VALUES(1,'a','b','c','d')"),
    ("sql", "UPDATE customers SET plan=load_extension('/tmp/evil')"),
    ("sql", "DROP TABLE sqlite_schema"),
    ("other", "SELECT 1"),
])
def test_unknown_commands_fail_closed(client, kind, command):
    result = submit(client, login(client), command, kind)
    assert result["status"] == result["classification"] == "blocked"
    assert client.get("/api/pending").json() == {"pending": []}


def test_sql_edge_cases(client):
    auth = login(client)
    assert submit(client, auth, "SELECT 'a;b' AS text;")["result"]["rows"] == [{"text": "a;b"}]
    assert submit(client, auth, "SELECT 1e999 AS n")["result"]["rows"] == [{"n": "inf"}]
    cte = submit(client, auth, "WITH c AS (SELECT id FROM customers) SELECT count(*) AS n FROM c")
    assert cte["result"]["rows"] == [{"n": 250}]


def test_auth_csrf_disposal_and_session_isolation(client):
    assert client.post("/session", headers={"X-Gate-Proxy-Token": "invalid"}, json={"access_code": CODE}).status_code == 403
    assert client.get("/api/audit").status_code == 401
    assert client.post("/session", headers={"Origin": "https://other.example"}, json={"access_code": CODE}).status_code == 403
    assert client.post("/session", json={"access_code": "incorrect"}).status_code == 401
    auth = login(client)
    first_cookie = client.cookies.get(app.SECURE_COOKIE)
    assert client.post("/action", json={"kind": "sql", "action": "SELECT 1"}).status_code == 403
    assert client.post("/action", headers={**auth, "Content-Type": "application/json"},
                       content=b"x" * 8193).status_code == 413
    submit(client, auth, "UPDATE customers SET plan='pro' WHERE id=1;")
    report = submit(client, auth, "DROP TABLE orders")
    pid = report["pending_id"]
    client.cookies.clear()
    other_auth = login(client)
    assert client.post(f"/approve/{pid}", headers=other_auth, json={}).status_code == 404
    assert client.post(f"/reject/{pid}", headers=auth, json={}).status_code == 403
    assert client.get("/api/audit").json() == {"audit": []}
    read = submit(client, other_auth, "SELECT plan FROM customers WHERE id=1")
    assert read["result"]["rows"] == [{"plan": "starter"}]
    client.cookies.clear()
    client.cookies.set(app.SECURE_COOKIE, first_cookie, domain="backend.example", path="/")
    assert client.post("/logout", headers=auth, json={}).status_code == 200
    assert client.get("/api/audit", headers={"Cookie": f"{app.SECURE_COOKIE}={first_cookie}"}).status_code == 401


def test_rate_limits_and_expiry(client, monkeypatch):
    auth = login(client)
    for _ in range(45):
        assert client.get("/api/audit").status_code == 200
    limited = client.get("/api/audit")
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
    start = app.monotonic()
    monkeypatch.setattr(app, "monotonic", lambda: start + app.SESSION_TTL + 1)
    assert client.post("/action", headers=auth, json={}).status_code == 401
    assert not app.app.state.sessions.active
    for _ in range(20):
        assert client.post("/session", json={"access_code": "bad"}).status_code == 401
    assert client.post("/session", json={"access_code": "bad"}).status_code == 429


def test_vultr_provider_response_and_fail_closed(client, monkeypatch):
    auth = login(client)
    assert client.post("/agent", headers=auth, json={"task": "Read fixtures"}).status_code == 502
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "test-vultr-key")
    monkeypatch.setenv("VULTR_INFERENCE_MODEL", "test-model")

    def fake_provider(request):
        assert str(request.url) == agent.URL
        assert request.headers["authorization"] == "Bearer test-vultr-key"
        return httpx.Response(200, json={
            "id": "test-completion", "model": "test-model",
            "choices": [{"finish_reason": "stop",
                         "message": {"content": '{"kind":"shell","action":"git reset --hard"}'}}],
        })

    client_class = httpx.AsyncClient
    monkeypatch.setattr(agent.httpx, "AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(fake_provider), **kwargs))
    reply = client.post("/agent", headers=auth, json={"task": "Propose a command"}).json()
    assert reply["inference"] == {
        "provider": "Vultr Serverless Inference", "model": "test-model", "completion_id": "test-completion",
    }
    assert reply["gate"]["status"] == "blocked"
    assert client.get("/api/audit").json()["audit"][0]["decision"] == "provider-response"


def test_vultr_safe_proposal_and_invalid_response(client, monkeypatch):
    auth = login(client)
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "test-vultr-key")
    monkeypatch.setenv("VULTR_INFERENCE_MODEL", "test-model")
    proposals = iter((
        '{"kind":"sql","action":"SELECT count(*) AS n FROM customers"}',
        '{"kind":"sql","action":["DROP TABLE orders"]}',
    ))

    def fake_provider(request):
        return httpx.Response(200, json={
            "id": "test-completion", "model": "test-model",
            "choices": [{"finish_reason": "stop", "message": {"content": next(proposals)}}],
        })

    client_class = httpx.AsyncClient
    monkeypatch.setattr(agent.httpx, "AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(fake_provider), **kwargs))
    result = client.post("/agent", headers=auth, json={"task": "Count the demo customers"})
    assert result.json()["gate"]["result"]["rows"] == [{"n": 250}]
    invalid = client.post("/agent", headers=auth, json={"task": "Malformed provider response"})
    assert invalid.status_code == 502
    assert "DROP" not in invalid.text
    assert client.get("/api/pending").json() == {"pending": []}


def test_legacy_form_fix(monkeypatch):
    monkeypatch.setenv("GATE_LOCAL_ORIGIN", "http://127.0.0.1:8000")
    monkeypatch.setenv("GATE_DEMO_ACCESS_CODE", CODE)
    with TestClient(app.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1234),
                    headers={"Origin": "http://127.0.0.1:8000"}) as local:
        assert local.post("/session", data={"access_code": CODE}, follow_redirects=False).status_code == 303
        html = local.get("/").text
        assert "submitJSON(event, this, 'actionOut'); return false;" in html
        assert 'fetch(form.getAttribute("action")' in html
        csrf = re.search(r'name="csrf" value="([^"]+)"', html)[1]
        form = {"csrf": csrf, "kind": "sql", "action": "DROP TABLE customers;"}
        report = local.post("/action", data=form).json()
        assert report["status"] == "pending-approval"
        assert local.post(f"/reject/{report['pending_id']}", data={"csrf": csrf},
                          follow_redirects=False).status_code == 303
        assert "rejected" in local.get("/").text


def repair_step(client, auth, step, **body):
    response = client.post(f"/api/repair/{step}", headers=auth, json=body)
    assert response.status_code == 200, response.text
    return response.json()["run"]


@pytest.mark.parametrize(("http_status", "finish_reason", "content", "truncated"), [
    (200, "length", '{"operation":"provider-private-content', True),
    (200, "length", '{"operation":"enable_sample_job"}', True),
    (200, "stop", '{"operation":"provider-private-content"', False),
    (200, "stop", '{"operation":"provider-private-content"}', False),
    (200, "stop", '{"operation":"enable_sample_job","sql":"provider-private-content"}', False),
    (200, "stop", '{"operation":"provider-private-content","operation":"enable_sample_job"}', False),
    (200, "tool_calls", '{"operation":"enable_sample_job"}', False),
    (200, None, '{"operation":"enable_sample_job"}', False),
    (429, None, "provider-private-content", False),
    (503, None, "provider-private-content", False),
    (200, "stop", '{"operation":"enable_sample_job"}', False),
])
def test_isolated_repair_proposal(client, monkeypatch, caplog, http_status, finish_reason, content, truncated):
    monkeypatch.setenv("VEYLIX_ISOLATED", "1")
    monkeypatch.setenv("VEYLIX_JUDGE_ORIGIN", "https://backend.example")
    client.headers["Origin"] = "https://backend.example"
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "test-vultr-key")
    monkeypatch.setenv("VULTR_INFERENCE_MODEL", "glm-5.3")
    monkeypatch.delenv(agent.REPAIR_TOKEN_ENV, raising=False)
    budget = 1024
    accepted = http_status == 200 and finish_reason == "stop" and content == '{"operation":"enable_sample_job"}'
    if accepted:
        budget = 1536
        monkeypatch.setenv(agent.REPAIR_TOKEN_ENV, str(budget))

    async def observe_only(op, job):
        assert (op, job) == ("observe", 7)
        return {"before": isolated.FAILED, "fixture_sha256": "a" * 64}, []

    def provider(request):
        assert str(request.url) == agent.URL
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == budget
        assert body["model"] == "glm-5.3"
        assert "max_tokens" not in body
        assert "7" in body["messages"][-1]["content"]
        choice = {"message": {"content": content}}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return httpx.Response(http_status, json={
            "id": "repair-completion", "model": "glm-5.3", "choices": [choice],
        })

    monkeypatch.setattr(isolated, "task", observe_only)
    client_class = httpx.AsyncClient
    monkeypatch.setattr(agent.httpx, "AsyncClient",
                        lambda **kwargs: client_class(transport=httpx.MockTransport(provider), **kwargs))
    auth = login(client)
    started = repair_step(client, auth, "start", job_id=7)
    run_id = started["run_id"]
    diagnosed = client.post("/api/repair/diagnose", headers=auth, json={"run_id": run_id})
    run = client.get("/api/repair").json()["run"]
    if accepted:
        assert diagnosed.status_code == 200
        assert run["status"] == "awaiting-approval"
        assert run["plan"]["operation"] == "enable_sample_job"
        assert run["plan"]["sql"] == repair.UPDATE
        assert run["plan"]["parameters"] == [7]
        assert run["audit"][-1]["evidence"]["inference"] == {
            "provider": "Vultr Serverless Inference", "completion_id": "repair-completion", "model": "glm-5.3",
        }
        assert repair_step(client, auth, "decide", run_id=run_id, decision="reject")["status"] == "rejected"
    else:
        assert diagnosed.status_code == 502
        assert ("finish_reason=length, max_completion_tokens=1024" in diagnosed.json()["detail"]) == truncated
        assert run == started
        assert client.post("/api/repair/decide", headers=auth,
                           json={"run_id": run_id, "decision": "approve"}).status_code == 409
    for sensitive in ("test-vultr-key", "provider-private-content", "Authorization"):
        assert sensitive not in diagnosed.text
        assert sensitive not in caplog.text


def prepare_repair(client, auth, job_id=7):
    run = repair_step(client, auth, "start", job_id=job_id)
    return repair_step(client, auth, "diagnose", run_id=run["run_id"])


def current_repair():
    return next(iter(app.app.state.sessions.active.values())).repair_run


def test_repair_measures_before_and_after_and_records_server_evidence(client):
    auth = login(client)
    assert client.get("/api/repair").json() == {"run": None}
    started = repair_step(client, auth, "start", job_id=17)
    run_id = started["run_id"]
    assert started["status"] == "failure-observed"
    assert started["before"] == {"status": "failed", "rows_returned": 0}
    assert started["plan"] is None
    assert client.post("/api/repair/decide", headers=auth,
                       json={"run_id": run_id, "decision": "approve"}).status_code == 409
    assert client.post("/api/repair/start", headers=auth, json={"job_id": 3}).status_code == 409
    diagnosed = repair_step(client, auth, "diagnose", run_id=run_id)
    assert diagnosed["status"] == "awaiting-approval"
    assert diagnosed["plan"]["sql"] == repair.UPDATE
    assert diagnosed["plan"]["parameters"] == [17]
    assert diagnosed["plan"]["expected_rows_updated"] == 1
    assert client.portal.call(lambda: current_repair().measure()) == started["before"]

    approved = repair_step(client, auth, "decide", run_id=run_id, decision="approve")
    assert approved["status"] == "verified"
    events = approved["audit"]
    assert [event["status"] for event in events] == [
        "failure-observed", "awaiting-approval", "approved", "verified",
    ]
    assert events[2]["evidence"]["plan"] == diagnosed["plan"]
    assert events[3]["evidence"] == {
        "rows_updated": 1, "before": {"status": "failed", "rows_returned": 0},
        "after": {"status": "passed", "rows_returned": 1}, "committed": True,
    }
    assert client.portal.call(lambda: current_repair().measure()) == events[3]["evidence"]["after"]
    assert len({event["event_id"] for event in events}) == 4
    assert len({event["actor"] for event in events}) == 1
    assert all(event["run_id"] == run_id and event["mode"] == "sqlite-fixture" for event in events)
    times = [datetime.fromisoformat(event["ts"]) for event in events]
    assert all(t.utcoffset().total_seconds() == 0 for t in times)
    assert times == sorted(times)
    readback = client.get("/api/repair")
    assert readback.json()["run"] == approved
    for secret in (CODE, TOKEN, auth["X-CSRF-Token"], client.cookies.get(app.SECURE_COOKIE)):
        assert secret not in readback.text


def test_repair_rejection_and_superseded_run(client):
    auth = login(client)
    run_id = prepare_repair(client, auth)["run_id"]
    rejected = repair_step(client, auth, "decide", run_id=run_id, decision="reject")
    assert rejected["status"] == "rejected"
    assert rejected["audit"][-1]["evidence"] == {"rows_updated": 0}
    assert client.portal.call(lambda: current_repair().measure()) == rejected["before"]
    payload = {"run_id": run_id, "decision": "approve"}
    assert client.post("/api/repair/decide", headers=auth, json=payload).status_code == 409
    fresh = prepare_repair(client, auth, job_id=8)
    assert fresh["run_id"] != run_id
    assert client.post("/api/repair/decide", headers=auth, json=payload).status_code == 404
    assert client.get("/api/repair").json()["run"] == fresh


def test_repair_approval_expires_without_execution(client, monkeypatch):
    auth = login(client)
    run = prepare_repair(client, auth)
    monkeypatch.setattr(repair, "monotonic", lambda: current_repair().approval_deadline)
    stale = repair_step(client, auth, "decide", run_id=run["run_id"], decision="approve")
    assert stale["status"] == "stale"
    assert [event["status"] for event in stale["audit"]] == ["failure-observed", "awaiting-approval", "stale"]
    assert client.portal.call(lambda: current_repair().measure()) == stale["before"]


def test_repair_execution_is_single_use_under_concurrency(client):
    auth = login(client)
    run_id = prepare_repair(client, auth)["run_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: client.post(
            "/api/repair/decide", headers=auth, json={"run_id": run_id, "decision": "approve"},
        ), range(2)))
    assert sorted(response.status_code for response in replies) == [200, 409]
    assert [event["status"] for event in client.get("/api/repair").json()["run"]["audit"]].count("verified") == 1


def test_repair_fails_closed_before_update(client, monkeypatch):
    auth = login(client)
    run_id = prepare_repair(client, auth)["run_id"]
    traced = []
    client.portal.call(lambda: current_repair().db.set_trace_callback(traced.append))

    def unavailable(_run):
        raise sqlite3.OperationalError("private diagnostic")

    monkeypatch.setattr(repair.Run, "job_is_disabled", unavailable)
    result = repair_step(client, auth, "decide", run_id=run_id, decision="approve")
    assert result["status"] == "repair-failed"
    failure = result["audit"][-1]["evidence"]
    assert failure["phase"] == "before-update"
    assert failure["attempted_rows_updated"] is None and not failure["committed"]
    assert not any(statement.startswith("UPDATE") for statement in traced)
    assert "private diagnostic" not in str(result)


def test_failed_rerun_rolls_back_an_actual_update(client, monkeypatch):
    auth = login(client)
    run_id = prepare_repair(client, auth)["run_id"]
    measure = repair.Run.measure

    def failed_rerun(run):
        assert measure(run) == {"status": "passed", "rows_returned": 1}
        raise sqlite3.OperationalError("rerun interrupted")

    monkeypatch.setattr(repair.Run, "measure", failed_rerun)
    result = repair_step(client, auth, "decide", run_id=run_id, decision="approve")
    assert result["status"] == "repair-failed"
    failure = result["audit"][-1]["evidence"]
    assert failure["phase"] == "verification" and failure["attempted_rows_updated"] == 1
    assert failure["after"] is None and not failure["committed"]
    assert client.portal.call(lambda: measure(current_repair())) == result["before"]
    assert client.post("/api/repair/decide", headers=auth,
                       json={"run_id": run_id, "decision": "approve"}).status_code == 409


@pytest.mark.parametrize("body", [
    {"job_id": "1; DROP TABLE repair_jobs"}, {"job_id": 21}, {"job_id": 0},
    {"job_id": True}, {"job_id": 7.0}, {"job_id": 7, "sql": "UPDATE customers SET plan='free'"},
])
def test_repair_rejects_inputs_outside_the_catalog(client, body):
    auth = login(client)
    assert client.post("/api/repair/start", headers=auth, json=body).status_code == 400
    assert client.get("/api/repair").json() == {"run": None}


def test_repair_requires_auth_origin_and_csrf_and_rejects_forged_evidence(client):
    assert client.get("/api/repair").status_code == 401
    auth = login(client)
    assert client.post("/api/repair/start", json={"job_id": 7}).status_code == 403
    wrong_origin = {**auth, "Origin": "https://other.example"}
    assert client.post("/api/repair/start", headers=wrong_origin, json={"job_id": 7}).status_code == 403
    run = prepare_repair(client, auth)
    forged = {"run_id": run["run_id"], "decision": "approve", "actor": "judge", "status": "verified"}
    assert client.post("/api/repair/decide", headers=auth, json=forged).status_code == 400
    assert client.get("/api/repair").json()["run"] == run
    for _ in range(45 - len(current_repair_session(client).requests)):
        assert client.get("/api/repair").status_code == 200
    assert client.get("/api/repair").status_code == 429


def current_repair_session(client):
    return app.app.state.sessions.active[client.cookies.get(app.SECURE_COOKIE)]


def test_repair_session_isolation_and_expiry(client, monkeypatch):
    first_auth = login(client)
    first = prepare_repair(client, first_auth)
    first_cookie = client.cookies.get(app.SECURE_COOKIE)
    client.cookies.clear()
    second_auth = login(client)
    assert client.get("/api/repair").json() == {"run": None}
    second = prepare_repair(client, second_auth)
    assert second["audit"][0]["actor"] != first["audit"][0]["actor"]
    foreign = {"run_id": first["run_id"], "decision": "approve"}
    assert client.post("/api/repair/decide", headers=second_auth, json=foreign).status_code == 404
    assert client.get("/api/repair").json()["run"] == second
    assert client.get("/api/repair", headers={"Cookie": f"{app.SECURE_COOKIE}={first_cookie}"}).json()["run"] == first

    second_db = current_repair_session(client).repair_run.db
    now = app.monotonic()
    monkeypatch.setattr(app, "monotonic", lambda: now + app.SESSION_TTL)
    assert client.get("/api/repair").status_code == 401
    assert client.post("/api/repair/decide", headers=second_auth, json={
        "run_id": second["run_id"], "decision": "approve",
    }).status_code == 401
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        client.portal.call(second_db.execute, "SELECT 1")
