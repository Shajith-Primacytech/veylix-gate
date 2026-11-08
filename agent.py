"""Vultr inference supplies a proposal. Only the gate can act on it."""
import json
import os

import httpx

from gate import MAX_ACTION

URL = "https://api.vultrinference.com/v1/chat/completions"
SYSTEM_PROMPT = (
    'Propose one action against disposable SQLite fixtures or simulated shell. '
    'Return only a JSON object with keys "kind" ("sql" or "shell") and "action" (a string). '
    'The SQLite fixture tables are customers, orders, and cloud_volumes. '
    'Prefer a read. Never include credentials or tool calls.'
)


async def propose(task: str) -> tuple[dict, dict]:
    return await completion(SYSTEM_PROMPT, task, legacy_proposal)


async def propose_repair(job_id: int) -> dict:
    _, evidence = await completion(
        'Diagnose a disposable SQLite fixture. The selected row exists with enabled=0. '
        'The workload selects the row only when enabled=1. Propose the narrow fix using '
        'only this JSON shape: {"operation":"enable_sample_job"}. No SQL or shell.',
        f"Job {job_id} returned zero rows; propose the narrow repair.",
        lambda payload: payload == {"operation": "enable_sample_job"},
    )
    return evidence


def legacy_proposal(payload):
    return (
        isinstance(payload, dict) and set(payload) == {"kind", "action"}
        and payload["kind"] in ("sql", "shell") and isinstance(payload["action"], str)
        and bool(payload["action"].strip()) and len(payload["action"]) <= MAX_ACTION
        and bool(payload["action"].encode())
    )


async def completion(system, task, valid):
    key = os.environ.get("VULTR_INFERENCE_API_KEY")
    model = os.environ.get("VULTR_INFERENCE_MODEL")
    if not key or not model:
        raise ValueError("Vultr Serverless Inference is not configured")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(URL, headers={"Authorization": f"Bearer {key}"}, json={
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": task}],
                "max_tokens": 256,
                "temperature": 0.1,
            })
        response.raise_for_status()
        body = response.json()
        payload = json.loads(body["choices"][0]["message"]["content"])
        if (
            not valid(payload)
            or not isinstance(body["id"], str) or not body["id"].strip()
            or not isinstance(body["model"], str) or not body["model"].strip()
        ):
            raise ValueError()
        body["id"].encode()
        body["model"].encode()
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise ValueError("Vultr inference failed or returned an invalid proposal") from None
    return payload, {
        "provider": "Vultr Serverless Inference",
        "completion_id": body["id"],
        "model": body["model"],
    }
