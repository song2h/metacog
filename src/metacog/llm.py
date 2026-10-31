"""Minimal Meta Model API client (OpenAI-compatible /v1/chat/completions), stdlib only."""

import json
import os
import urllib.error
import urllib.request

from metacog import store

BASE_URL = os.environ.get("METACOG_BASE_URL", "https://api.meta.ai/v1")


def api_key() -> str | None:
    store.load_env()
    return os.environ.get("MODEL_API_KEY") or os.environ.get("META_API_KEY")


def chat_json(model: str, system: str, user: str, schema: dict, timeout: float = 90) -> tuple[dict, dict]:
    """Return (parsed JSON content, usage). Raises RuntimeError on API or parse failure."""
    key = api_key()
    if not key:
        raise RuntimeError("no MODEL_API_KEY; run `metacog init`")
    body = {
        "model": model,
        "reasoning_effort": os.environ.get("METACOG_EFFORT", "low"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": schema.get("title", "result"), "schema": schema}},
    }
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Meta API {e.code}: {e.read()[:300]!r}") from e
    content = data["choices"][0]["message"]["content"]
    try:
        return json.loads(content), data.get("usage", {})
    except json.JSONDecodeError as e:
        raise RuntimeError(f"verifier returned non-JSON: {content[:200]!r}") from e
