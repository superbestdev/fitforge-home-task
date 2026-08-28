"""LLM client.

Speaks the OpenAI chat-completions protocol, which every serious open-source
inference server implements — Ollama, vLLM, llama.cpp, TGI, LM Studio. Moving
between them is an `.env` change. Nothing above this module knows what is
actually serving the tokens.

Two things this module insists on:

**Structured output is schema-constrained, not prompted.** Asking a 3B model to
"reply with JSON only" and then parsing whatever comes back is the single most
common source of flakiness in small-model agents. Ollama supports a JSON-schema
`format` that constrains decoding, so malformed output becomes impossible rather
than merely unlikely.

**Every call is metered.** Token counts and latency land in `llm_calls`, keyed by
node. That table is what makes the cost model in docs/09-cost-model.md real
numbers instead of estimates, and what shows which node is burning the budget.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from openai import APIError, APITimeoutError

from ..config import settings
from ..db import execute

log = logging.getLogger(__name__)

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "not-needed",
            timeout=float(settings.llm_timeout_s),
            max_retries=0,          # retries are handled here, with logging
        )
    return _client


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    model: str
    ok: bool = True
    error: str | None = None

    def json(self) -> dict:
        """Parse the response as JSON, tolerating a stray code fence."""
        raw = self.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)


def complete(
    *,
    system: str,
    user: str,
    node: str,
    session_id: str | None = None,
    issue_id: str | None = None,
    model: str | None = None,
    schema: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int = 700,
) -> LLMResponse:
    """One chat completion, metered and retried.

    Pass `schema` (a JSON Schema) to constrain decoding. The response is then
    guaranteed parseable, so callers do not need defensive parsing.
    """
    use_model = model or settings.llm_model
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    kwargs: dict[str, Any] = {
        "model": use_model,
        "messages": messages,
        "temperature": (settings.llm_temperature if temperature is None else temperature),
        "max_tokens": max_tokens,
    }
    if schema is not None:
        # The OpenAI-standard structured-output form. Ollama, vLLM and TGI all
        # implement it on their compatible endpoints, so this constrains
        # decoding identically whichever one is serving.
        #
        # Note: Ollama's native `format` field is NOT plumbed through its
        # OpenAI-compatible route — passing it there is silently ignored and the
        # model returns a bare JSON string instead of an object.
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": node, "strict": True, "schema": schema},
        }

    last_error: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        started = time.monotonic()
        try:
            resp = client().chat.completions.create(**kwargs)
            latency_ms = int((time.monotonic() - started) * 1000)

            text = (resp.choices[0].message.content or "").strip()
            usage = resp.usage
            out = LLMResponse(
                text=text,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                latency_ms=latency_ms,
                model=use_model,
            )
            _record(out, node, session_id, issue_id)
            return out

        except (APITimeoutError, APIError) as exc:
            last_error = exc
            latency_ms = int((time.monotonic() - started) * 1000)
            log.warning("LLM call failed (node=%s attempt=%d/%d): %s",
                        node, attempt + 1, settings.llm_max_retries + 1, exc)
            if attempt == settings.llm_max_retries:
                break
            time.sleep(1.5 * (attempt + 1))

        except Exception as exc:                        # noqa: BLE001
            last_error = exc
            log.exception("unexpected LLM failure (node=%s)", node)
            break

    failed = LLMResponse(
        text="", prompt_tokens=0, completion_tokens=0, latency_ms=0,
        model=use_model, ok=False, error=str(last_error)[:500],
    )
    _record(failed, node, session_id, issue_id)
    return failed


def complete_json(
    *,
    system: str,
    user: str,
    node: str,
    schema: dict[str, Any],
    fallback: dict,
    session_id: str | None = None,
    issue_id: str | None = None,
    model: str | None = None,
    max_tokens: int = 700,
) -> dict:
    """Schema-constrained completion that always returns a dict.

    On any failure it returns `fallback`. Callers get a usable value in every
    case; whether the model actually answered is visible in `_llm_ok`, and the
    nodes use that to decide whether to count a tool failure.
    """
    resp = complete(
        system=system, user=user, node=node, session_id=session_id,
        issue_id=issue_id, model=model, schema=schema, max_tokens=max_tokens,
    )
    if not resp.ok:
        return {**fallback, "_llm_ok": False, "_error": resp.error}

    try:
        parsed = resp.json()
        if not isinstance(parsed, dict):
            raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
        # Ollama's `format` constrains the *shape* of the output but does not
        # enforce JSON Schema `required`, so a small model will happily return a
        # valid object that omits a key the caller depends on. Layering the
        # fallback underneath means callers can index the result unconditionally
        # rather than each node defending itself.
        merged = {**fallback, **{k: v for k, v in parsed.items() if v is not None}}
        merged["_llm_ok"] = True
        return merged
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("node=%s returned unparseable JSON despite schema: %s | raw=%.200s",
                    node, exc, resp.text)
        return {**fallback, "_llm_ok": False, "_error": f"unparseable: {exc}"}


def _record(resp: LLMResponse, node: str, session_id: str | None,
            issue_id: str | None) -> None:
    """Meter the call. Never let accounting break the request path."""
    try:
        execute(
            """
            INSERT INTO llm_calls (session_id, issue_id, node, model,
                                   prompt_tokens, completion_tokens,
                                   latency_ms, ok, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (session_id, issue_id, node, resp.model, resp.prompt_tokens,
             resp.completion_tokens, resp.latency_ms, resp.ok, resp.error),
        )
    except Exception:                                   # pragma: no cover
        log.debug("could not record llm_calls row", exc_info=True)


def healthy() -> bool:
    try:
        client().models.list()
        return True
    except Exception:                                   # pragma: no cover
        return False
