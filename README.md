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

In the existing scenario picker, approving a pending action records a simulated outcome. Rejecting it records a block. Both consume the pending item, and a fixture change invalidates earlier approval reports. Shell and cloud commands are simulated. The demo has no shell executor, cloud mutation credentials, or production database connection. A Docker service hosts the API.

## Judge walkthrough

1. On the approved Vercel Preview, select **Try a Live Repair** and enter the demo access code provided by the organizer.
2. Choose a sample job from 1 through 20 and select **Create and run broken sample**. The backend seeds a private in-memory SQLite database and actually runs the sample query. The disabled job returns zero rows.
3. Select **Request diagnosis**. Review the exact parameterized UPDATE, its bound job ID, and the expected one-row effect. Review expires after five minutes.
4. Select **Approve and rerun**. The backend applies the one-row UPDATE, reruns the same SELECT, and commits only if both measured results match the plan. Expect **Repair verified and committed**, with zero rows before, one row after, and one row updated. To see the reject path, create another sample workload and reject its plan. Rejection leaves the sample job disabled.
5. Open the server audit timeline and download the redacted JSON before replacing the run or ending the session. It includes UTC timestamps, server-generated event and run IDs, a pseudonymous session actor, the plan, the measured result, and the `sqlite-fixture` mode. The export is an unsigned snapshot of the server's session record and omits credentials and cookies.

This is a real SQL repair of sample data in a session-scoped SQLite fixture. Shell and cloud scenarios are simulations. The guided workflow uses a fixed SQL catalog, one current run per session, the existing session, Origin and CSRF checks, and the existing request limits. It needs the updated API on the isolated staging VM and the updated frontend on Vercel Preview with the staging gateway URL, matching gateway token, and the exact Preview origin on the API allowlist. No inference key or OS runner is needed for the guided workflow.

**Judge launch gate:** NO-GO until the walkthrough succeeds on the actual Preview URL connected to the isolated staging backend. Confirm that a second browser session cannot read or approve the first run, and that the downloaded evidence matches the server timeline. Local tests and a local build alone do not clear this gate.

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

### Access prerequisites

- A separately provisioned Vultr staging VM and a dedicated staging administrator. Use the Vultr web console to install the administrator's public SSH key and verify the VM's SSH host-key fingerprint out of band. The administrator needs staging-only Docker and firewall administration. Perform deployment and SSH troubleshooting on the staging VM.
- A DNS record for `gate-staging.<owned-domain>` pointing to the staging VM, inbound TCP 80 and 443 for Caddy, and SSH restricted to the administrator's network. Permit outbound HTTPS to the image registry, ACME, and the official Vultr inference endpoint. The FastAPI port stays private.
- Access to a demo-only Vultr Serverless Inference subscription and one enabled chat model. Vercel project access must allow Preview deployments and Preview environment variables in `veylix-gate-demo`.
- A GitHub checkout of the reviewed feature branch, with its exact commit verified. Pushing a Git-connected branch can trigger a Vercel deployment. Confirm the target and Preview configuration before pushing.

### Isolated deployment

1. Check out the feature branch on the staging VM in its own directory. Use the separate Compose project name `veylix-staging`. Its containers, network, and Caddy volumes are project-scoped. FastAPI uses in-memory fixture databases and has no data mount.
2. Store the VM settings in a root-owned environment file outside the checkout, such as `/etc/veylix-gate/staging.env` with mode `0600`. Enter values through your authorized staging VM session using `sudoedit` or your existing secrets system. Generate independent random values for the proxy token and demo access code. Share the access code only through the team's approved secret-sharing channel.
3. From the staging checkout, validate, build, and start the isolated stack.

   ```sh
   sudo docker compose --env-file /etc/veylix-gate/staging.env -p veylix-staging config --quiet
   sudo docker compose --env-file /etc/veylix-gate/staging.env -p veylix-staging build --pull gate
   sudo docker compose --env-file /etc/veylix-gate/staging.env -p veylix-staging up -d
   ```

   Use the quiet configuration check to avoid rendering environment values. From outside the VM, verify `https://gate-staging.<owned-domain>/healthz` returns 200 over trusted TLS and direct `/api/audit` access returns 403. The Compose stack publishes only Caddy's ports 80 and 443.
4. In `veylix-gate-demo` → Settings → Environment Variables, add the two gateway variables to **Preview**. Mark the gateway token sensitive and scope it to the feature branch where supported. Verify the Preview builds `web` with the **Other** framework preset and a supported Node.js runtime. For a CLI-managed project, link the `web` directory to the existing project and deploy from there. Leave project-wide production settings alone.
5. Once the staging API and Preview variables are confirmed, deploy the reviewed feature build to **Preview**. If deploying from the CLI, use `vercel deploy --environment preview` and verify the returned deployment target. If the Preview URL is assigned at deploy time, start the VM with an empty effective origin allowlist, such as `GATE_ALLOWED_ORIGINS=https://unassigned.invalid`. Set the exact assigned Preview HTTPS origin in the staging environment file and recreate only the staging `gate` service before testing authenticated requests.

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

Record the commit, Preview URL, staging API URL, HTTP status codes, fixture count, and provider completion evidence. Keep cookies, CSRF tokens, access codes, and gateway tokens out of the report.


- Check that the Vultr HTTPS certificate is trusted and direct access to `/api/audit` returns 403. Check that the preview requires a demo session and refuses a mutation with a missing CSRF token or a different Origin.
- Sign in and run safe SELECT, one-row UPDATE, DROP rejection, and mock volume approval. For an independent read-only count, run `await api("action", {kind: "sql", action: "SELECT COUNT(*) AS customer_count FROM customers;"})` in the signed-in Preview browser console. Check for `customer_count: 250`. Confirm the DROP report shows the SQLite foreign-key failure and the audit shows the rejection. Confirm mock volume approval reads `approved-simulated`.
- Start a separate browser session and verify that fixture changes, pending items, and audit history from the first session are private. End a session and confirm its old cookie fails.
- In the preview's **Ask Vultr Serverless Inference** form, request exactly `SELECT COUNT(*) AS customer_count FROM customers;` as a read-only SQL proposal. Record the returned model, completion ID, gate classification, and returned fixture count. Check the session audit for `provider-response` and the SQL outcome. An absent key, provider error, or invalid proposal returns an error without running an action. Test mocks exercise this contract locally. Only a successful preview request demonstrates live inference.

Review these results before promoting. See the [Vercel function runtime](https://vercel.com/docs/functions/runtimes/node-js) and [Caddy TLS requirements](https://caddyserver.com/docs/automatic-https).
