"""Thin agent loop over Vultr Serverless Inference (OpenAI-compatible)."""
import os
from openai import OpenAI

import gate

SYSTEM_PROMPT = """You are an AI coding agent helping with a staging environment issue.
When you decide to run something, respond with a single JSON object on one line:
{"kind": "sql" | "shell", "action": "<the exact command or SQL>"}
Do not run anything yourself. The gate runs it. Prefer the smallest action.
"""


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("VULTR_INFERENCE_API_KEY", "sk-no-key"),
        base_url=os.environ.get("VULTR_INFERENCE_BASE_URL", "https://api.vultrinference.com/v1"),
    )


def ask_agent(user_task: str) -> dict:
    """Ask the model for one action, run it through the gate."""
    import json
    try:
        resp = _client().chat.completions.create(
            model=os.environ.get("VULTR_INFERENCE_MODEL", "llama-3.3-70b-instruct"),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_task},
            ],
            temperature=0.2,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        return {"ok": False, "error": f"inference call failed: {e}", "task": user_task}

    import re
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"ok": False, "error": "model returned no JSON", "raw": raw}
    try:
        payload = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"bad JSON from model: {e}", "raw": raw}

    kind = payload.get("kind")
    action = payload.get("action")
    if kind not in ("sql", "shell") or not action:
        return {"ok": False, "error": "model gave invalid kind/action", "payload": payload}

    gate_result = gate.decide_and_route(kind, action)
    return {"ok": True, "agent_raw": raw, "proposed": payload, "gate": gate_result}
