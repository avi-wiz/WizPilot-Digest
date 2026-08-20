"""Provider-agnostic LLM call, stdlib only.

Two wire formats supported, auto-selected by which env vars are set:

  OpenAI-compatible (Grok, a LiteLLM proxy, OpenAI itself):
    LLM_BASE_URL + LLM_API_KEY [+ LLM_MODEL]
    POST {base}/chat/completions, Authorization: Bearer <key>,
    body {"model", "messages": [{"role":"system"|"user", ...}]},
    reply at choices[0].message.content.

  Anthropic Messages API (direct, no proxy):
    ANTHROPIC_API_KEY [+ ANTHROPIC_MODEL]
    POST https://api.anthropic.com/v1/messages,
    headers x-api-key + anthropic-version (NOT Authorization: Bearer —
    a real difference from OpenAI-shaped APIs, not a typo),
    body {"model", "max_tokens", "system": <string, top-level, not a
    messages entry>, "messages": [{"role":"user", ...}]},
    reply at content[0].text.

ANTHROPIC_API_KEY takes priority if both are set, so switching providers is
an env var change, not a code change, in either direction.
"""
from __future__ import annotations
import json
import os
import urllib.request

ANTHROPIC_VERSION = "2023-06-01"  # required header; bump only if you need a
                                  # newer Messages API feature — this value
                                  # is what was current when this was written


class LLMError(RuntimeError):
    pass


def active_provider() -> str:
    """Which provider chat() will actually use, for logging — uses the exact
    same priority check as chat() itself, so this can't drift out of sync
    with the real routing decision."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        return f"Anthropic ({model})"
    if os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY"):
        model = os.environ.get("LLM_MODEL", "grok-4")
        return f"OpenAI-compatible ({model}) via {os.environ['LLM_BASE_URL']}"
    return "NONE CONFIGURED"


def chat(system: str, user_content: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    """Returns the raw text reply. Raises LLMError on any failure — callers
    already wrap this in try/except with a template fallback, so this stays
    a plain exception rather than swallowing anything itself.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        return _chat_anthropic(system, user_content, max_tokens, temperature, anthropic_key)

    base_url, key = os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_API_KEY")
    if base_url and key:
        return _chat_openai_compatible(system, user_content, max_tokens, temperature, base_url, key)

    raise LLMError("No LLM configured: set ANTHROPIC_API_KEY, or LLM_BASE_URL+LLM_API_KEY.")


def _chat_openai_compatible(system, user_content, max_tokens, temperature, base_url, key) -> str:
    payload = json.dumps({
        "model": os.environ.get("LLM_MODEL", "grok-4"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user_content}],
        "temperature": temperature, "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(f"{base_url.rstrip('/')}/chat/completions", data=payload)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"OpenAI-compatible call failed: {e}") from e
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected OpenAI-compatible response shape: {resp}") from e


def _chat_anthropic(system, user_content, max_tokens, temperature, key) -> str:
    payload = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,                                    # top-level, NOT a messages entry
        "messages": [{"role": "user", "content": user_content}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload)
    req.add_header("x-api-key", key)                          # NOT "Authorization: Bearer"
    req.add_header("anthropic-version", ANTHROPIC_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Anthropic call failed: {e}") from e
    try:
        return resp["content"][0]["text"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected Anthropic response shape: {resp}") from e


def strip_code_fence(raw: str) -> str:
    """Both providers occasionally wrap JSON in ```json fences despite being
    told not to. Shared so all three call sites handle it identically.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()
