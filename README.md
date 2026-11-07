# Veylix Gate

Blast-radius gate for AI coding agents. Built for the Vultr Agent Rush hackathon
(Challenge 1 — Blast Radius Zero), November 3–8, 2026.

Every destructive action an agent proposes is intercepted, dry-run against a
database snapshot, and held until a human sees the blast radius and approves.

## Architecture

- **Vultr Cloud Compute (one VM)** — hosts everything
- **Vultr Serverless Inference** — the agent's LLM calls (OpenAI-compatible endpoint)
- **Docker** — one container per agent run; no secrets, outbound allowlist
- **FastAPI + Jinja** — gate, API, approval UI
- **sqlglot + shlex** — rule-based classification (SAFE / REVERSIBLE / DESTRUCTIVE)
- **SQLite** — seeded demo database with customers, orders, cloud volumes

## Classification rules

| Class | Examples | Behaviour |
| --- | --- | --- |
| Safe | `SELECT`, `ls`, `cat`, `git …` | Runs immediately, logged |
| Reversible | `INSERT`, `UPDATE … WHERE …` | Runs, snapshotted |
| Destructive | `DROP`, `TRUNCATE`, `DELETE` without `WHERE`, `rm -rf`, `vultr volume delete` | Dry-run, blast-radius report, human approval |

Unclassifiable → treated as destructive.

## Quick start (local)

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
# open http://localhost:8000
```

## Deploy on the Vultr VM

```bash
git clone https://github.com/<owner>/veylix-gate.git /root/veylix-gate
cd /root/veylix-gate
export VULTR_INFERENCE_API_KEY=...
docker compose up -d --build
```

The UI is on port 8000. Put Caddy in front of it for HTTPS on your domain.

## Demo (PocketOS replay, ~90s)

1. Safe action (SELECT customers) → runs instantly.
2. Destructive SQL (`DROP TABLE customers;`) → blocked, report shows 250 customers + 1,200 orders would be lost.
3. Destructive cloud CLI (`vultr volume delete vol_prod_db_01`) → blocked, report names the production volume and that backups go with it.
4. Reversible fix (`UPDATE customers SET plan='pro' WHERE id=1`) → approved and runs.

## Submission

- Vultr Cloud Compute VM: 144.202.88.88
- Vultr Serverless Inference, OpenAI-compatible endpoint: api.vultrinference.com/v1
- Public repo: this one
- Live URL: added once Caddy is set up
- MIT-licensed
