"""Optional, opt-in OpenAI extraction. Local deterministic processing remains the fallback."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .storage import get_setting


def ai_capability_label() -> str:
    if str(get_setting("VENDORFLOW_ENABLE_AI", "false")).strip().lower() not in {"1", "true", "yes"}:
        return "AI extraction disabled; local PDF text rules are active."
    if not get_setting("OPENAI_API_KEY"):
        return "AI extraction unavailable; OPENAI_API_KEY is not configured."
    return f"AI extraction enabled ({get_setting('OPENAI_MODEL', 'gpt-4o-mini')}); source matching is enforced."


def extract_with_ai(text: str) -> tuple[dict[str, Any], str]:
    """Request JSON candidates and accept source-matched candidates only."""
    if str(get_setting("VENDORFLOW_ENABLE_AI", "false")).strip().lower() not in {"1", "true", "yes"}:
        return {}, "disabled"
    api_key = get_setting("OPENAI_API_KEY")
    if not api_key:
        return {}, "unavailable: API key missing"
    model = str(get_setting("OPENAI_MODEL", "gpt-4o-mini"))
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Extract literal values from the document text. Return JSON object with keys registered_company_name, gstin, pan, account_holder_name, bank_name, ifsc. Use null for absent fields. Never infer or correct values."},
            {"role": "user", "content": text[:18000]},
        ],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        result = json.loads(content)
        fields = {key: value for key, value in result.items() if isinstance(value, str) and value.strip()}
        return fields, "available"
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return {}, "unavailable: request or response error"
