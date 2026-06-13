"""LLMWorker: drives one OpenRouter chat call per `act()`, parses the
worker envelope, and handles the two failure modes that warrant in-worker
recovery (429 rate-limit and schema-parse failure).

Failure semantics:
  - HTTP 429 on the primary model -> retry once with the fallback model (if
    configured) and emit a `model_swapped` event. If no fallback, or the
    fallback also 429s, raise a `tool_failed` alarm with
    `error_kind='rate_limited'` and return Escalate.
  - Schema-parse failure -> retry ONCE with a repair message appended; if
    still bad, raise `output_schema_violation` and return Escalate.
  - LLMTransportError (non-429) -> raise `tool_failed` with
    `error_kind='transport'`, return Escalate.

Spend-log policy (consistent across all paths):
  - Each SUCCESSFUL API call produces exactly one `spend_log` row.
  - Failed API calls (429, transport) produce NO spend_log row. Rationale:
    OpenRouter does not bill failed requests, so logging a zero-cost row
    would only add noise and break the "row count == successful calls"
    invariant the UI relies on for the swap badge.
  - `is_fallback` is set true on every row whose model == the fallback id,
    regardless of which attempt (primary-then-fallback-success,
    primary-success-then-fallback-repair-success, etc.).

The system prompt + envelope contract + few-shots are NOT this module's job
— the domain bundle injects them via `WorkerContext.system_prompt`. We trust
the prompt and parse what comes back.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import ValidationError

from harness.models.enums import EventType
from harness.models.envelope import (
    Escalate,
    Final,
    Message,
    ToolCall,
    WorkerContext,
    parse_worker_response,
)
from harness.services import alarms, store
from harness.services.llm import (
    ChatResponse,
    LLMTransportError,
    OpenRouterClient,
    RateLimited,
)


_RESPONSE_FORMAT_JSON: dict = {"type": "json_object"}
_RAW_PREVIEW_MAX = 500


class LLMWorker:
    """Worker that talks to a single OpenRouter model (with optional fallback).

    Implements the structural `Worker` protocol from `services.worker`. The
    orchestrator does not know whether it has a real or mock worker.
    """

    def __init__(
        self,
        *,
        primary: str,
        fallback: str | None,
        llm_client: OpenRouterClient,
        core_conn: sqlite3.Connection,
        session_conn: sqlite3.Connection,
        session_id: str,
    ) -> None:
        self._primary = primary
        # Treat empty string the same as None — env-driven config often
        # produces "" when the variable is set but unset to no value.
        self._fallback = fallback if fallback else None
        self._client = llm_client
        self._core_conn = core_conn
        self._session_conn = session_conn
        self._session_id = session_id

    def act(self, ctx: WorkerContext) -> ToolCall | Final | Escalate:
        """One turn: convert context -> OpenRouter call -> typed envelope."""
        api_messages = _to_api_messages(ctx.messages)

        # First attempt: primary model. On 429, try the fallback (if any).
        first_chat = self._call_with_swap(
            messages=api_messages,
            ctx=ctx,
        )
        if first_chat is None:
            # _call_with_swap already raised the alarm and returned None as
            # the signal for "transport-level give-up: escalate."
            return Escalate(reason="rate limited" if self._last_failure_kind == "rate_limited" else "transport error")

        chat_response, model_used, is_fallback = first_chat

        # Try to parse the envelope. On failure, attempt a single repair retry.
        parse_result = _try_parse(chat_response.text)
        if isinstance(parse_result, _ParseOk):
            return parse_result.envelope

        # Schema repair retry. Reuses whichever model just answered (fallback
        # if we already swapped, else primary). That way a swap that succeeds
        # transport-wise still gets a repair chance on its own output.
        repair_messages = api_messages + [
            _repair_message(parse_result.error),
        ]
        repair_chat = self._call_one(
            model=model_used,
            messages=repair_messages,
            is_fallback=is_fallback,
            ctx=ctx,
        )
        if repair_chat is None:
            # Repair attempt itself blew up transport-side. Treat as escalation
            # under the same kind we recorded in `_last_failure_kind`.
            return Escalate(
                reason=(
                    "rate limited"
                    if self._last_failure_kind == "rate_limited"
                    else "transport error"
                )
            )

        repair_parse = _try_parse(repair_chat.text)
        if isinstance(repair_parse, _ParseOk):
            return repair_parse.envelope

        # Two parse failures in a row — raise the schema alarm and escalate.
        alarms.raise_output_schema_violation(
            self._session_conn,
            parse_error=repair_parse.error,
            repair_attempt=1,
            raw_text_preview=repair_chat.text[:_RAW_PREVIEW_MAX],
            stage=ctx.stage,
        )
        return Escalate(reason="schema")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    _last_failure_kind: Literal["rate_limited", "transport", "ok"] = "ok"

    def _call_with_swap(
        self,
        *,
        messages: list[dict],
        ctx: WorkerContext,
    ) -> tuple[ChatResponse, str, bool] | None:
        """Issue the primary call; on 429, attempt the fallback once.

        Returns (chat_response, model_used, is_fallback) on success or None
        when both attempts fail. Failure side-effects (alarms, events) are
        emitted here so callers can stay flat.
        """
        try:
            chat_response = self._client.chat(
                model=self._primary,
                messages=messages,
                response_format=_RESPONSE_FORMAT_JSON,
            )
        except RateLimited:
            return self._swap_to_fallback(messages=messages, ctx=ctx)
        except LLMTransportError as exc:
            self._last_failure_kind = "transport"
            alarms.raise_tool_failed(
                self._session_conn,
                tool="llm.chat",
                args={"model": self._primary},
                error_kind="transport",
                error_message=str(exc),
                stage=ctx.stage,
            )
            return None

        self._log_spend(chat_response, model=self._primary, is_fallback=False)
        self._last_failure_kind = "ok"
        return chat_response, self._primary, False

    def _swap_to_fallback(
        self,
        *,
        messages: list[dict],
        ctx: WorkerContext,
    ) -> tuple[ChatResponse, str, bool] | None:
        """Handle the primary-429 branch: emit model_swapped + retry fallback."""
        if self._fallback is None:
            self._last_failure_kind = "rate_limited"
            alarms.raise_tool_failed(
                self._session_conn,
                tool="llm.chat",
                args={"model": self._primary},
                error_kind="rate_limited",
                error_message=(
                    f"primary model {self._primary} returned 429 and no"
                    " fallback is configured"
                ),
                stage=ctx.stage,
            )
            return None

        # Emit the swap event BEFORE the fallback call so it appears at the
        # right spot in the timeline even if the fallback also fails.
        store.append_event(
            self._session_conn,
            type=EventType.MODEL_SWAPPED.value,
            stage=ctx.stage,
            payload={
                "from": self._primary,
                "to": self._fallback,
                "reason": "rate_limited",
            },
        )

        try:
            chat_response = self._client.chat(
                model=self._fallback,
                messages=messages,
                response_format=_RESPONSE_FORMAT_JSON,
            )
        except RateLimited:
            self._last_failure_kind = "rate_limited"
            alarms.raise_tool_failed(
                self._session_conn,
                tool="llm.chat",
                args={"model": self._fallback},
                error_kind="rate_limited",
                error_message=(
                    f"fallback model {self._fallback} also returned 429 after"
                    f" primary {self._primary} 429"
                ),
                stage=ctx.stage,
            )
            return None
        except LLMTransportError as exc:
            self._last_failure_kind = "transport"
            alarms.raise_tool_failed(
                self._session_conn,
                tool="llm.chat",
                args={"model": self._fallback},
                error_kind="transport",
                error_message=str(exc),
                stage=ctx.stage,
            )
            return None

        self._log_spend(chat_response, model=self._fallback, is_fallback=True)
        self._last_failure_kind = "ok"
        return chat_response, self._fallback, True

    def _call_one(
        self,
        *,
        model: str,
        messages: list[dict],
        is_fallback: bool,
        ctx: WorkerContext,
    ) -> ChatResponse | None:
        """Single shot at a specific model — used by the repair retry.

        No swap logic here: if the repair call itself 429s or fails
        transport-side, we raise the appropriate alarm and bail. We do NOT
        try to swap during repair — that's a quality trade-off (the first
        bad parse is more likely a prompt issue than a model issue, and
        adding another swap layer makes the failure path noisy).
        """
        try:
            chat_response = self._client.chat(
                model=model,
                messages=messages,
                response_format=_RESPONSE_FORMAT_JSON,
            )
        except RateLimited:
            self._last_failure_kind = "rate_limited"
            alarms.raise_tool_failed(
                self._session_conn,
                tool="llm.chat",
                args={"model": model},
                error_kind="rate_limited",
                error_message=(
                    f"model {model} returned 429 during repair retry"
                ),
                stage=ctx.stage,
            )
            return None
        except LLMTransportError as exc:
            self._last_failure_kind = "transport"
            alarms.raise_tool_failed(
                self._session_conn,
                tool="llm.chat",
                args={"model": model},
                error_kind="transport",
                error_message=str(exc),
                stage=ctx.stage,
            )
            return None

        self._log_spend(chat_response, model=model, is_fallback=is_fallback)
        self._last_failure_kind = "ok"
        return chat_response

    def _log_spend(
        self,
        response: ChatResponse,
        *,
        model: str,
        is_fallback: bool,
    ) -> None:
        """One row per successful API call. Sole spend writer in the worker."""
        store.record_spend(
            self._core_conn,
            session_id=self._session_id,
            model=model,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
            is_fallback=is_fallback,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _to_api_messages(messages: list[Message]) -> list[dict]:
    """Convert envelope Messages to OpenAI-format dicts.

    `tool_call_id` is dropped: v1 uses JSON-mode chat, not OpenAI's tool-call
    API. Tool results are conveyed as user messages by the orchestrator.
    """
    return [{"role": m.role, "content": m.content} for m in messages]


def _repair_message(parse_error: str) -> dict:
    """Build the single repair-retry user message."""
    return {
        "role": "user",
        "content": (
            "Your previous response was not valid JSON matching the envelope"
            f" schema. Error: {parse_error}. Reply ONLY with a valid envelope"
            " JSON object."
        ),
    }


class _ParseOk:
    """Sentinel for a successful envelope parse."""

    __slots__ = ("envelope",)

    def __init__(self, envelope: ToolCall | Final | Escalate) -> None:
        self.envelope = envelope


class _ParseFail:
    """Sentinel for a failed envelope parse with the readable error."""

    __slots__ = ("error",)

    def __init__(self, error: str) -> None:
        self.error = error


def _try_parse(raw_text: str) -> _ParseOk | _ParseFail:
    """Attempt to parse the worker envelope; never raises."""
    try:
        envelope = parse_worker_response(raw_text)
    except ValidationError as exc:
        return _ParseFail(_summarize_validation_error(exc))
    except (json.JSONDecodeError, ValueError) as exc:
        return _ParseFail(str(exc))
    return _ParseOk(envelope)


def _summarize_validation_error(exc: ValidationError) -> str:
    """Compact, human-readable summary fit for an alarm context field."""
    parts: list[str] = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "validation failed"


__all__ = ["LLMWorker"]
