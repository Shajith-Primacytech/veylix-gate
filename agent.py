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
    key = os.environ.get("VULTR_INFERENCE_API_KEY")
    model = os.environ.get("VULTR_INFERENCE_MODEL")
    if not key or not model:
        raise ValueError("Vultr Serverless Inference is not configured")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(URL, headers={"Authorization": f"Bearer {key}"}, json={
                "model": model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}],
                "max_tokens": 256,
                "temperature": 0.1,
            })
        response.raise_for_status()
        body = response.json()
        payload = json.loads(body["choices"][0]["message"]["content"])
        if (
            not isinstance(payload, dict) or set(payload) != {"kind", "action"}
            or payload["kind"] not in ("sql", "shell") or not isinstance(payload["action"], str)
            or not payload["action"].strip() or len(payload["action"]) > MAX_ACTION
            or not isinstance(body["id"], str) or not body["id"].strip()
            or not isinstance(body["model"], str) or not body["model"].strip()
        ):
            raise ValueError()
        payload["action"].encode()
        body["id"].encode()
        body["model"].encode()
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise ValueError("Vultr inference failed or returned an invalid proposal") from None
    return payload, {
        "provider": "Vultr Serverless Inference",
        "completion_id": body["id"],
        "model": body["model"],
    }
