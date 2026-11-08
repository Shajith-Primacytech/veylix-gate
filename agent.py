"""Vultr inference supplies a proposal. Only the gate can act on it."""
import json
import os

import httpx

from gate import MAX_ACTION

URL = "https://api.vultrinference.com/v1/chat/completions"
REPAIR_TOKEN_ENV = "VULTR_REPAIR_MAX_COMPLETION_TOKENS"
DEFAULT_REPAIR_TOKENS = 1024
SYSTEM_PROMPT = (
    'Propose one action against disposable SQLite fixtures or simulated shell. '
    'Return only a JSON object with keys "kind" ("sql" or "shell") and "action" (a string). '
    'The SQLite fixture tables are customers, orders, and cloud_volumes. '
    'Prefer a read. Never include credentials or tool calls.'
)


class InferenceTruncated(ValueError):
    def __init__(self, max_completion_tokens: int):
        super().__init__(
            f"Vultr inference truncated the proposal "
            f"(finish_reason=length, max_completion_tokens={max_completion_tokens})"
        )


async def propose(task: str) -> tuple[dict, dict]:
    return await completion(SYSTEM_PROMPT, task, legacy_proposal)


async def propose_repair(job_id: int) -> dict:
    try:
        budget = int(os.environ.get(REPAIR_TOKEN_ENV, str(DEFAULT_REPAIR_TOKENS)))
        if budget < 1:
            raise ValueError()
    except ValueError:
        raise ValueError(f"{REPAIR_TOKEN_ENV} must be a positive integer") from None
    _, evidence = await completion(
        'Return only {"operation":"enable_sample_job"} to propose enabling the disabled sample job. '
        'No explanation, markdown, SQL, or shell.',
        f"Sample job {job_id} is disabled and the workload returns zero rows.",
        lambda payload: payload == {"operation": "enable_sample_job"},
        max_completion_tokens=budget,
    )
    return evidence


def legacy_proposal(payload):
    return (
        isinstance(payload, dict) and set(payload) == {"kind", "action"}
        and payload["kind"] in ("sql", "shell") and isinstance(payload["action"], str)
        and bool(payload["action"].strip()) and len(payload["action"]) <= MAX_ACTION
        and bool(payload["action"].encode())
    )


def unique_json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return result


async def completion(system, task, valid, *, max_completion_tokens=256):
    key = os.environ.get("VULTR_INFERENCE_API_KEY")
    model = os.environ.get("VULTR_INFERENCE_MODEL")
    if not key or not model:
        raise ValueError("Vultr Serverless Inference is not configured")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(URL, headers={"Authorization": f"Bearer {key}"}, json={
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": task}],
                "max_completion_tokens": max_completion_tokens,
                "temperature": 0.1,
            })
        response.raise_for_status()
        body = response.json()
        choice = body["choices"][0]
        if choice["finish_reason"] == "length":
            raise InferenceTruncated(max_completion_tokens)
        if choice["finish_reason"] != "stop":
            raise ValueError()
        payload = json.loads(choice["message"]["content"], object_pairs_hook=unique_json_object)
        if (
            not valid(payload)
            or not isinstance(body["id"], str) or not body["id"].strip()
            or not isinstance(body["model"], str) or not body["model"].strip()
        ):
            raise ValueError()
        body["id"].encode()
        body["model"].encode()
    except InferenceTruncated:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        raise ValueError("Vultr inference failed or returned an invalid proposal") from None
    return payload, {
        "provider": "Vultr Serverless Inference",
        "completion_id": body["id"],
        "model": body["model"],
    }
