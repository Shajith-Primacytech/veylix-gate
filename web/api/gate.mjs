const readable = new Set(["session", "api/pending", "api/audit"]);
const writable = new Set(["session", "logout", "action", "agent"]);
const maxBody = 8192;
const noCache = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
};

function fail(status, detail) {
  return Response.json({ detail }, { status, headers: noCache });
}

async function boundedBody(request) {
  const reader = request.body?.getReader();
  if (!reader) return new Uint8Array();
  const chunks = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) return Buffer.concat(chunks, size);
    size += value.length;
    if (size > maxBody) {
      void reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const route = url.searchParams.get("route");
    const post = request.method === "POST";
    const allowed = post
      ? writable.has(route) || /^(approve|reject)\/[1-9][0-9]{0,8}$/.test(route ?? "")
      : request.method === "GET" && readable.has(route);
    if (!allowed) return fail(404, "Demo route not found");
    if (url.protocol !== "https:" || (post && request.headers.get("origin") !== url.origin)) {
      return fail(403, "HTTPS and same-origin requests required");
    }

    let upstream;
    const token = process.env.GATE_PROXY_TOKEN;
    try {
      const configured = process.env.GATE_BACKEND_ORIGIN;
      upstream = new URL(configured);
      if (upstream.protocol !== "https:" || upstream.origin !== configured || !token || token.length < 32) {
        throw new Error("Invalid demo gateway configuration");
      }
    } catch {
      return fail(503, "Demo gateway is not configured");
    }

    const headers = new Headers({ "x-gate-proxy-token": token, Accept: "application/json" });
    const cookie = request.headers.get("cookie")?.split(";").map((part) => part.trim())
      .find((part) => /^__Host-veylix-demo=[A-Za-z0-9_-]{43}$/.test(part));
    if (cookie) headers.set("Cookie", cookie);
    const csrf = request.headers.get("x-csrf-token");
    if (csrf) headers.set("x-csrf-token", csrf);

    try {
      let body;
      if (post) {
        if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") {
          return fail(415, "Send application/json");
        }
        body = await boundedBody(request);
        if (!body) return fail(413, "Request body is too large");
        headers.set("Origin", url.origin);
        headers.set("Content-Type", "application/json");
      }
      const response = await fetch(new URL("/" + route, upstream), {
        method: request.method,
        headers,
        body,
        redirect: "manual",
        signal: AbortSignal.timeout(15000),
      });
      if (!response.headers.get("content-type")?.startsWith("application/json") ||
          (response.status >= 300 && response.status < 400)) {
        return fail(502, "Invalid response from demo backend");
      }
      const outgoing = new Headers({ ...noCache, "Content-Type": "application/json" });
      const sessionCookie = response.headers.get("set-cookie");
      if (sessionCookie?.startsWith("__Host-veylix-demo=") && ["session", "logout"].includes(route)) {
        outgoing.set("Set-Cookie", sessionCookie);
      }
      const retry = response.headers.get("retry-after");
      if (retry) outgoing.set("Retry-After", retry);
      return new Response(response.body, { status: response.status, headers: outgoing });
    } catch {
      return fail(502, "Demo backend is unavailable");
    }
  },
};
