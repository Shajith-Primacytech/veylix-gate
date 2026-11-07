import assert from "node:assert/strict";
import test from "node:test";
import gateway from "../web/api/gate.mjs";

const origin = "https://frontend.example";
const upstream = "https://backend.example";
const secret = "fake-test-proxy-secret-".repeat(3);

function request(route, init, base = origin) {
  return new Request(base + "/api/gate?route=" + encodeURIComponent(route), init);
}

function post(route, headers = {}, body = "{}", base = origin) {
  return request(route, {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/json", ...headers },
    body,
  }, base);
}

test("proxy permits only exact routes and HTTPS same-origin mutations", async (t) => {
  const forward = t.mock.method(globalThis, "fetch", () => { throw new Error("unexpected fetch"); });
  assert.equal((await gateway.fetch(post("https://attacker.example"))).status, 404);
  assert.equal((await gateway.fetch(post("approve/../session"))).status, 404);
  assert.equal((await gateway.fetch(request("approve/1"))).status, 404);
  assert.equal((await gateway.fetch(post("approve/1", { Origin: "https://attacker.example" }))).status, 403);
  assert.equal((await gateway.fetch(post("session", {}, "{}", "http://frontend.example"))).status, 403);
  assert.equal(forward.mock.callCount(), 0);
});

test("proxy forwards scoped cookies and sets its own upstream credential", async (t) => {
  const cookie = "__Host-veylix-demo=" + "a".repeat(43);
  t.mock.method(globalThis, "fetch", (url, init) => {
    assert.equal(String(url), upstream + "/session");
    assert.equal(init.headers.get("x-gate-proxy-token"), secret);
    assert.equal(init.headers.get("x-csrf-token"), "test-csrf");
    assert.equal(init.headers.get("origin"), origin);
    assert.equal(init.headers.get("cookie"), cookie);
    assert.equal(init.headers.get("authorization"), null);
    assert.equal(init.redirect, "manual");
    return Response.json({ csrf: "test-csrf" }, {
      status: 201,
      headers: { "Set-Cookie": cookie + "; Secure; HttpOnly; SameSite=Strict; Path=/" },
    });
  });
  const response = await gateway.fetch(post("session", {
    Cookie: "other=secret; " + cookie, "X-Gate-Proxy-Token": "forged",
    Authorization: "discard", "X-CSRF-Token": "test-csrf",
  }));
  assert.equal(response.status, 201);
  assert.match(response.headers.get("set-cookie"), /Secure; HttpOnly; SameSite=Strict/);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("x-gate-proxy-token"), null);
});

test("proxy rejects oversized bodies and unsafe upstreams or responses", async (t) => {
  const forward = t.mock.method(globalThis, "fetch", () => new Response("<script>bad()</script>"));
  assert.equal((await gateway.fetch(post("action", {}, "x".repeat(8193)))).status, 413);
  assert.equal(forward.mock.callCount(), 0);
  assert.equal((await gateway.fetch(post("action"))).status, 502);
  process.env.GATE_BACKEND_ORIGIN = "http://backend.example";
  assert.equal((await gateway.fetch(post("action"))).status, 503);
});

process.env.GATE_PROXY_TOKEN = secret;
process.env.GATE_BACKEND_ORIGIN = upstream;
