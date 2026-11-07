# Veylix Gate

A blast-radius demo for startup engineering teams and indie developers. The Sleek Glass frontend on Vercel sends same-origin requests through a Node function to FastAPI on Vultr Cloud Compute. The function authenticates to the Vultr origin over HTTPS using a server-only gateway token. Vultr Serverless Inference can propose actions through the same gate.

Each authenticated session starts with 250 fictional customers, 1,200 orders, and two mock volumes in an in-memory SQLite database. The session also owns its pending approvals and audit history. Sessions expire after 30 minutes and are discarded on logout or process restart. Run one FastAPI worker on one instance for this demo.

| Proposal | Demo behavior |
| --- | --- |
| Allowlisted SELECT | Reads fixture data with query time and output limits |
| Primary-key scoped UPDATE or DELETE, plain INSERT values | Previews in a copy, then applies to the session fixtures if valid |
| Broader mutation or allowlisted DROP TABLE | Runs only against a disposable copy for impact analysis and waits for review |
| Allowlisted destructive shell or cloud scenario | Reports mock impact and waits for review |
| Unknown shell or SQL syntax, multiple statements, unsupported SQL functions or tables | Blocks without an approval option |

Approve records a simulated outcome. Reject records a block. Both consume the pending item, and a fixture change invalidates earlier approval reports. Shell and cloud commands are simulated. The demo has no shell executor, cloud mutation credentials, or production database connection. A Docker service hosts the API. Shell sandbox isolation is outside this demo.

## Local checks

Use Python 3.12 and Node.js 24.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt pytest
python -m pytest -q
node --test tests/proxy.test.mjs
```

To use the existing Jinja form locally, generate a temporary demo access code, then start FastAPI on loopback. Enter the value from your local secret store at the operator form.

```sh
export GATE_LOCAL_ORIGIN=http://127.0.0.1:8000
# Supply GATE_DEMO_ACCESS_CODE from your local secret store.
uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

The local operator mode is intended for a loopback listener. The packaged deployment uses the HTTPS gateway.

## Stage the feature branch

1. Use a separate checkout of `feat/startup-premium-vercel-demo` and a separate Compose project on the demo VM. Keep the existing checkout, containers, and database intact. Point a dedicated API DNS name to the VM and configure a trusted certificate on the TLS listener. The provided Caddy service can obtain a public certificate when it owns ports 80 and 443. If the VM already has a TLS listener, add a separate host to it and route to the demo FastAPI service through a private Docker network or a loopback-only port.
2. Store the VM settings in a root-owned environment file outside the checkout, such as `/etc/veylix-gate/preview.env` with mode `0600`. Enter values through your authorized VM session using `sudoedit` or your existing secrets system. Generate independent random values for the proxy token and demo access code. Share the access code only through the team's approved secret-sharing channel.
3. When the VM has free ports 80 and 443, start the demo with `sudo docker compose --env-file /etc/veylix-gate/preview.env -p veylix-preview up -d --build`. For an existing TLS listener, start only `gate` with the private routing described above. The API service in the supplied Compose file publishes zero host ports. Keep its port private.
4. Configure the Vercel project with **Root Directory** set to `web`, **Framework Preset** set to **Other**, and Node.js 24. In Project Settings → Environment Variables, set the two Vercel variables for **Preview** and scope them to the feature branch where supported. Keep the inference key on the VM. Build the linked preview with `vercel build` and stage the feature-branch preview after reviewing the target. Add its exact HTTPS origin to the VM allowlist before opening the demo.

| Location | Variable | Value |
| --- | --- | --- |
| Vultr VM | `GATE_DEMO_ACCESS_CODE` | Independent random demo operator code, at least 32 characters |
| Vultr VM and Vercel Preview | `GATE_PROXY_TOKEN` | Same random token on both sides, at least 32 characters |
| Vultr VM | `GATE_ALLOWED_ORIGINS` | Exact HTTPS preview origin, or comma-separated exact origins without spaces or trailing slashes |
| Vultr VM | `GATE_BACKEND_HOST` | Bare API hostname for the supplied Caddy service |
| Vercel Preview | `GATE_BACKEND_ORIGIN` | Exact HTTPS origin for that API hostname, without a trailing slash |
| Vultr VM, inference only | `VULTR_INFERENCE_API_KEY` | API key from a demo-only Vultr Serverless Inference subscription |
| Vultr VM, inference only | `VULTR_INFERENCE_MODEL` | Exact model ID enabled for that subscription |

For inference, open **Compute → Serverless Inference** in the Vultr Console and select the demo subscription. Use its API key and an enabled chat model from that subscription. Put the key and model in the VM environment file above. The provider URL is fixed to Vultr's official HTTPS chat completions endpoint. [Vultr chat documentation](https://docs.vultr.com/products/compute/serverless-inference/management/usage/chat)

## Preview acceptance checks

- Check that the Vultr HTTPS certificate is trusted and direct access to `/api/audit` returns 403. Check that the preview requires a demo session and refuses a mutation with a missing CSRF token or a different Origin.
- Sign in and run safe SELECT, one-row UPDATE, DROP rejection, and mock volume approval. Confirm the DROP report shows the SQLite foreign-key failure and the audit shows the rejection. Confirm mock volume approval reads `approved-simulated`.
- Start a separate browser session and verify that fixture changes, pending items, and audit history from the first session are private. End a session and confirm its old cookie fails.
- In the preview's **Ask Vultr Serverless Inference** form, request a read-only count of demo customers. Record the returned model, completion ID, gate classification, and returned fixture count. Check the session audit for `provider-response` and the SQL outcome. An absent key, provider error, or invalid proposal returns an error without running an action. Test mocks exercise this contract locally. Only a successful preview request demonstrates live inference.

Review these results before promoting. See the [Vercel function runtime](https://vercel.com/docs/functions/runtimes/node-js) and [Caddy TLS requirements](https://caddyserver.com/docs/automatic-https).
