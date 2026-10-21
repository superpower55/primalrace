"""LLM client abstraction.

Three providers, chosen via PROVIDER env var (default "ollama"):
  - "anthropic"  → Claude (Fable 5.1 by default), with prompt caching
  - "ollama"     → Ollama Cloud or self-hosted Ollama (Gemma 4 31B by default)
  - "openai"     → any OpenAI-compatible endpoint (Google AI Studio, Groq, …)

All providers return a dict:
  {text, input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, model}

Caching tokens are 0 for Ollama (no caching there).
"""
from __future__ import annotations

import json
import os
import urllib.request

PROVIDER = (os.environ.get("PROVIDER") or "ollama").lower()


# ============================================================================
# Anthropic
# ============================================================================

# Fable thinks before answering and thinking counts against max_tokens, so
# the callers' budgets (sized for the visible JSON) get this much headroom.
ANTHROPIC_THINKING_HEADROOM = 14000


def _anthropic_generate(system_blocks: list[dict], user_msg: str,
                        max_tokens: int = 2000,
                        json_schema: dict | None = None) -> dict:
    """Fable 5.1 notes: thinking is always on (no `thinking` param), sampling
    params like temperature are rejected, and safety classifiers may decline
    with stop_reason "refusal" — `fallbacks: "default"` reruns a declined
    request on Anthropic's recommended fallback model inside the same call.

      ANTHROPIC_MODEL   default claude-fable-5-1
      ANTHROPIC_EFFORT  low | medium | high | xhigh | max (default medium)
      ANTHROPIC_WORKSPACE_ID  needed only for keys not scoped to a workspace
    """
    import anthropic
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(
        default_headers={"anthropic-workspace-id": workspace} if workspace else None,
    )
    model = os.environ.get("ANTHROPIC_MODEL") or "claude-fable-5-1"
    effort = os.environ.get("ANTHROPIC_EFFORT") or "medium"
    output_config = {"effort": effort}
    if json_schema:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}
    resp = client.beta.messages.create(
        model=model,
        max_tokens=max_tokens + ANTHROPIC_THINKING_HEADROOM,
        system=system_blocks,
        messages=[{"role": "user", "content": user_msg}],
        output_config=output_config,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        category = resp.stop_details.category if resp.stop_details else None
        raise RuntimeError(f"{resp.model} declined the request (category={category})")
    text = "".join(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    return {
        "text": text,
        "model": resp.model,  # differs from `model` when a fallback served it
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


# ============================================================================
# Ollama (Cloud at https://ollama.com or self-hosted)
# ============================================================================

def _ollama_generate(system_blocks: list[dict], user_msg: str,
                     max_tokens: int = 2000) -> dict:
    """Call /api/chat. Combines the multi-block system prompt into one string —
    Ollama has no caching so the structure doesn't matter to it."""
    base_url = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")
    api_key = os.environ.get("OLLAMA_API_KEY", "")
    model = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")

    system_text = "\n\n".join(b["text"] for b in system_blocks)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": "json",          # ask Ollama to constrain output to valid JSON
        "options": {
            "temperature": 0.7,
            "num_predict": max_tokens,
        },
    }

    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode("utf-8"))

    text = data.get("message", {}).get("content", "")
    return {
        "text": text,
        "model": model,
        "input_tokens": data.get("prompt_eval_count", 0) or 0,
        "output_tokens": data.get("eval_count", 0) or 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
    }


# ============================================================================
# OpenAI-compatible (Groq, Google AI Studio, OpenRouter, ollama /v1, …)
# ============================================================================

def _openai_generate(system_blocks: list[dict], user_msg: str,
                     max_tokens: int = 2000) -> dict:
    """POST {base}/chat/completions — works with any OpenAI-compatible host.

    Env:
      OPENAI_BASE_URL  e.g. https://api.groq.com/openai/v1
                       or https://generativelanguage.googleapis.com/v1beta/openai
      OPENAI_API_KEY
      OPENAI_MODEL     e.g. llama-3.3-70b-versatile / gemini-2.0-flash
    """
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not base_url or not model:
        raise ValueError("PROVIDER=openai needs OPENAI_BASE_URL and OPENAI_MODEL")

    system_text = "\n\n".join(b["text"] for b in system_blocks)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "primalrace-news/1.0",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            # 503 = provider capacity spike, 429 = per-minute rate limit —
            # both are transient, back off and retry
            if e.code in (429, 503) and attempt < 2:
                import time
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"{base_url} HTTP {e.code}: {body}") from e

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {}) or {}
    return {
        "text": text,
        "model": model,
        "input_tokens": usage.get("prompt_tokens", 0) or 0,
        "output_tokens": usage.get("completion_tokens", 0) or 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
    }


# ============================================================================
# Dispatch
# ============================================================================

def generate(system_blocks: list[dict], user_msg: str,
             max_tokens: int = 2000, json_schema: dict | None = None) -> dict:
    """json_schema: enforced via structured outputs on Anthropic; the other
    providers rely on the prompt (Ollama additionally forces generic JSON)."""
    if PROVIDER == "anthropic":
        return _anthropic_generate(system_blocks, user_msg, max_tokens, json_schema)
    if PROVIDER == "ollama":
        return _ollama_generate(system_blocks, user_msg, max_tokens)
    if PROVIDER == "openai":
        return _openai_generate(system_blocks, user_msg, max_tokens)
    raise ValueError(f"unknown PROVIDER={PROVIDER!r}; "
                     f"use 'anthropic', 'ollama' or 'openai'")


def provider_label() -> str:
    if PROVIDER == "anthropic":
        return f"anthropic/{os.environ.get('ANTHROPIC_MODEL') or 'claude-fable-5-1'}"
    if PROVIDER == "ollama":
        host = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
        return f"ollama@{host}/{os.environ.get('OLLAMA_MODEL', 'gemma4:31b-cloud')}"
    if PROVIDER == "openai":
        host = os.environ.get("OPENAI_BASE_URL", "?")
        return f"openai@{host}/{os.environ.get('OPENAI_MODEL', '?')}"
    return PROVIDER
