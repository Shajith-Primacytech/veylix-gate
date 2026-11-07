import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

import agent
import app

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
            "choices": [{"message": {"content": '{"kind":"shell","action":"git reset --hard"}'}}],
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
            "choices": [{"message": {"content": next(proposals)}}],
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
