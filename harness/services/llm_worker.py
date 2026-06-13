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

# Language-drift guardrail thresholds. Trigger a one-shot English-strict
# retry when the response contains a meaningful proportion of CJK characters
# (Chinese / Japanese / Korean). The threshold is permissive enough to NOT
# flag an envelope that quotes a CJK business name in a single field (a name
# of ~5 chars is ~0.5% of a 1000-char response) but catches the failure mode
# where the entire body is in a non-English language and would never parse
# as the JSON envelope downstream.
_CJK_REJECT_MIN_COUNT = 8
_CJK_REJECT_MIN_RATIO = 0.03
_ENGLISH_STRICT_DIRECTIVE = (
    "CRITICAL: Reply in ENGLISH ONLY. Your response must be a JSON envelope"
    " as previously specified, with English keys and English values. Do not"
    " switch languages."
)


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
        """One turn: convert context -> OpenRouter call -> typed envelope.

        Failure modes are handled with two INDEPENDENT one-shot retries in a
        fixed order, so a worst-case turn issues up to FOUR API calls:

          1. initial call (primary, with 429 swap to fallback if configured)
          2. language-drift retry (if response is heavy CJK)
          3. envelope parse
          4. repair retry (if parse failed)

        The CJK check runs BEFORE envelope parse so a Chinese response is
        logged as `language_violation` rather than misclassified as
        `parse_error` — they're distinct failure modes in the audit log.
        """
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

        chat_response, model_used, is_fallback, first_call_id = first_chat

        # Language-drift gate. If the response is heavy CJK, swap in the
        # retry's (response, model, is_fallback, call_id) tuple for the rest
        # of the parse pipeline. On terminal language violation, escalate
        # directly without ever attempting a parse.
        gated = self._enforce_language(
            api_messages=api_messages,
            chat_response=chat_response,
            model_used=model_used,
            is_fallback=is_fallback,
            call_id=first_call_id,
            ctx=ctx,
        )
        if gated is None:
            return Escalate(reason="language drift")
        chat_response, model_used, is_fallback, first_call_id = gated

        # Try to parse the envelope. On failure, attempt a single repair retry.
        parse_result = _try_parse(chat_response.text)
        if isinstance(parse_result, _ParseOk):
            return parse_result.envelope

        # The first call's API attempt succeeded transport-wise but failed
        # envelope parse. Re-tag its llm_calls row as `repair_retry` so the
        # audit trail records "this call triggered the repair attempt."
        self._retag_llm_call(first_call_id, "repair_retry")

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

        repair_response, _repair_call_id = repair_chat
        repair_parse = _try_parse(repair_response.text)
        if isinstance(repair_parse, _ParseOk):
            return repair_parse.envelope

        # Two parse failures in a row — raise the schema alarm and escalate.
        # Re-tag the second attempt as `parse_error` so the llm_calls log
        # shows a terminal-parse outcome rather than a generic `ok`.
        self._retag_llm_call(
            _repair_call_id,
            "parse_error",
            error_message=repair_parse.error,
        )
        alarms.raise_output_schema_violation(
            self._session_conn,
            parse_error=repair_parse.error,
            repair_attempt=1,
            raw_text_preview=repair_response.text[:_RAW_PREVIEW_MAX],
            stage=ctx.stage,
        )
        return Escalate(reason="schema")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    _last_failure_kind: Literal["rate_limited", "transport", "ok"] = "ok"

    def _enforce_language(
        self,
        *,
        api_messages: list[dict],
        chat_response: ChatResponse,
        model_used: str,
        is_fallback: bool,
        call_id: str,
        ctx: WorkerContext,
    ) -> tuple[ChatResponse, str, bool, str] | None:
        """Run the CJK guardrail; on violation, attempt ONE English-strict retry.

        Returns the (possibly-replaced) (response, model, is_fallback, call_id)
        tuple to use for the rest of the parse pipeline, or ``None`` when the
        language violation is terminal (retry also CJK / retry blew up
        transport-side). On terminal failure this method has already raised
        the appropriate alarm.
        """
        ratio, count = _cjk_char_ratio(chat_response.text)
        if count < _CJK_REJECT_MIN_COUNT and ratio < _CJK_REJECT_MIN_RATIO:
            return chat_response, model_used, is_fallback, call_id

        # First-attempt language violation: re-tag the row so the audit log
        # captures the failure mode distinctly from parse_error.
        self._retag_llm_call(
            call_id,
            "language_violation",
            error_message=f"CJK chars: {count} ({ratio:.2%})",
        )

        # One-shot English-strict retry on the same model that just answered.
        # No swap layer here — the issue is content, not transport. Mirrors
        # the repair-retry policy.
        retry_messages = api_messages + [_english_directive_message()]
        retry_chat = self._call_one(
            model=model_used,
            messages=retry_messages,
            is_fallback=is_fallback,
            ctx=ctx,
        )
        if retry_chat is None:
            # The retry itself failed transport-side (`_call_one` already
            # raised the appropriate `tool_failed` alarm). Escalate without
            # raising a duplicate language alarm.
            return None

        retry_response, retry_call_id = retry_chat
        retry_ratio, retry_count = _cjk_char_ratio(retry_response.text)
        if (
            retry_count >= _CJK_REJECT_MIN_COUNT
            or retry_ratio >= _CJK_REJECT_MIN_RATIO
        ):
            # Two language violations in a row. Re-tag the second row and
            # raise `output_schema_violation` with `error_kind='language_violation'`
            # — language drift is a kind of output contract violation; we
            # intentionally do NOT add a new alarm type for v1.
            self._retag_llm_call(
                retry_call_id,
                "language_violation",
                error_message=(
                    f"CJK chars: {retry_count} ({retry_ratio:.2%});"
                    " English-strict retry also failed."
                ),
            )
            alarms.raise_output_schema_violation(
                self._session_conn,
                parse_error=(
                    f"language_violation: response contained {retry_count}"
                    f" CJK chars ({retry_ratio:.2%}); language retry also"
                    " failed."
                ),
                repair_attempt=2,
                raw_text_preview=retry_response.text[:_RAW_PREVIEW_MAX],
                stage=ctx.stage,
            )
            return None

        # Retry succeeded the CJK check — hand its response back to the
        # parse pipeline. The row stays `ok` (parse may still re-tag it).
        return retry_response, model_used, is_fallback, retry_call_id

    def _call_with_swap(
        self,
        *,
        messages: list[dict],
        ctx: WorkerContext,
    ) -> tuple[ChatResponse, str, bool, str] | None:
        """Issue the primary call; on 429, attempt the fallback once.

        Returns (chat_response, model_used, is_fallback, llm_call_id) on
        success or None when both attempts fail. Failure side-effects
        (alarms, events) are emitted here so callers can stay flat. Every
        API attempt (success OR failure) writes one ``llm_calls`` row; the
        returned id is the row for the SUCCESSFUL call so the act() loop
        can re-tag it later (e.g. ``repair_retry`` when parse fails).
        """
        try:
            chat_response = self._client.chat(
                model=self._primary,
                messages=messages,
                response_format=_RESPONSE_FORMAT_JSON,
            )
        except RateLimited as exc:
            # Record the 429 attempt before the swap branch runs.
            self._record_llm_call_failure(
                messages=messages,
                model=self._primary,
                is_fallback=False,
                status="rate_limited",
                error_message=str(exc),
            )
            return self._swap_to_fallback(messages=messages, ctx=ctx)
        except LLMTransportError as exc:
            self._record_llm_call_failure(
                messages=messages,
                model=self._primary,
                is_fallback=False,
                status="transport_error",
                error_message=str(exc),
            )
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
        call_id = self._record_llm_call_success(
            messages=messages,
            response=chat_response,
            model=self._primary,
            is_fallback=False,
        )
        self._last_failure_kind = "ok"
        return chat_response, self._primary, False, call_id

    def _swap_to_fallback(
        self,
        *,
        messages: list[dict],
        ctx: WorkerContext,
    ) -> tuple[ChatResponse, str, bool, str] | None:
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
        except RateLimited as exc:
            self._record_llm_call_failure(
                messages=messages,
                model=self._fallback,
                is_fallback=True,
                status="rate_limited",
                error_message=str(exc),
            )
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
            self._record_llm_call_failure(
                messages=messages,
                model=self._fallback,
                is_fallback=True,
                status="transport_error",
                error_message=str(exc),
            )
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
        call_id = self._record_llm_call_success(
            messages=messages,
            response=chat_response,
            model=self._fallback,
            is_fallback=True,
        )
        self._last_failure_kind = "ok"
        return chat_response, self._fallback, True, call_id

    def _call_one(
        self,
        *,
        model: str,
        messages: list[dict],
        is_fallback: bool,
        ctx: WorkerContext,
    ) -> tuple[ChatResponse, str] | None:
        """Single shot at a specific model — used by the repair retry.

        Returns ``(response, llm_call_id)`` on transport-success so the
        caller can re-tag the row's status when envelope parsing fails.

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
        except RateLimited as exc:
            self._record_llm_call_failure(
                messages=messages,
                model=model,
                is_fallback=is_fallback,
                status="rate_limited",
                error_message=str(exc),
            )
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
            self._record_llm_call_failure(
                messages=messages,
                model=model,
                is_fallback=is_fallback,
                status="transport_error",
                error_message=str(exc),
            )
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
        call_id = self._record_llm_call_success(
            messages=messages,
            response=chat_response,
            model=model,
            is_fallback=is_fallback,
        )
        self._last_failure_kind = "ok"
        return chat_response, call_id

    def _log_spend(
        self,
        response: ChatResponse,
        *,
        model: str,
        is_fallback: bool,
    ) -> None:
        """Append a row to the core-DB ``spend_log``.

        We keep this side-by-side with ``record_llm_call`` because the two
        tables serve different consumers:
          - ``spend_log`` (core DB) drives the cross-session rolling $1/day
            cap query, which runs on every turn — staying lean is essential.
          - ``llm_calls`` (per-session DB) is the full-payload audit log used
            by the session detail UI; bigger rows, but scoped to one session.
        Removing either would break a documented contract — keep both.
        """
        store.record_spend(
            self._core_conn,
            session_id=self._session_id,
            model=model,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
            is_fallback=is_fallback,
        )

    # ------------------------------------------------------------------
    # llm_calls audit log
    # ------------------------------------------------------------------

    def _record_llm_call_success(
        self,
        *,
        messages: list[dict],
        response: ChatResponse,
        model: str,
        is_fallback: bool,
    ) -> str:
        """Persist a successful API attempt to the per-session llm_calls log."""
        return store.record_llm_call(
            self._session_conn,
            model=model,
            is_fallback=is_fallback,
            request_messages=messages,
            request_options=_request_options_for_log(),
            response_text=response.text,
            finish_reason=None,  # ChatResponse doesn't carry this today
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
            status="ok",
            related_event_id=store.latest_worker_input_event_id(
                self._session_conn
            ),
        )

    def _record_llm_call_failure(
        self,
        *,
        messages: list[dict],
        model: str,
        is_fallback: bool,
        status: str,
        error_message: str,
    ) -> str:
        """Persist a failed (429 / transport) API attempt with zeroed usage."""
        return store.record_llm_call(
            self._session_conn,
            model=model,
            is_fallback=is_fallback,
            request_messages=messages,
            request_options=_request_options_for_log(),
            response_text=None,
            finish_reason=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status=status,
            error_message=error_message,
            related_event_id=store.latest_worker_input_event_id(
                self._session_conn
            ),
        )

    def _retag_llm_call(
        self,
        call_id: str,
        new_status: str,
        *,
        error_message: str | None = None,
    ) -> None:
        """Update an existing llm_calls row's status (and optional error).

        Used after envelope parsing decisions: when an ``ok`` row needs to
        become ``repair_retry`` (parse failed but the API call itself was
        fine) or ``parse_error`` (final attempt's parse also failed).
        """
        if error_message is None:
            self._session_conn.execute(
                "UPDATE llm_calls SET status = ? WHERE id = ?",
                (new_status, call_id),
            )
        else:
            self._session_conn.execute(
                "UPDATE llm_calls SET status = ?, error_message = ?"
                " WHERE id = ?",
                (new_status, error_message, call_id),
            )
        self._session_conn.commit()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _request_options_for_log() -> dict:
    """Return the chat-call options (response_format, temperature) for logging.

    Mirrors the values LLMWorker passes to ``OpenRouterClient.chat`` so the
    llm_calls audit row captures the full request shape. The default
    temperature (0.2) lives in ``OpenRouterClient.chat`` — we duplicate it
    here so the log row is self-describing; if either side changes, update
    both. ``temperature`` is part of the request, not the response, so it
    belongs in request_options.
    """
    return {
        "response_format": _RESPONSE_FORMAT_JSON,
        "temperature": 0.2,
    }


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


def _english_directive_message() -> dict:
    """Build the single English-strict retry system message.

    Sent as ``role='system'`` because it's a hard contract directive, not
    user content. The orchestrator's stub-friendly message conversion in
    `_to_api_messages` preserves the role verbatim.
    """
    return {"role": "system", "content": _ENGLISH_STRICT_DIRECTIVE}


def _cjk_char_ratio(text: str) -> tuple[float, int]:
    """Return ``(ratio, count)`` of CJK characters in ``text``.

    Counts characters in the following Unicode blocks:
      - CJK Unified Ideographs (U+4E00..U+9FFF) — common Chinese / Japanese kanji
      - CJK Unified Ideographs Extension A (U+3400..U+4DBF)
      - Hiragana (U+3040..U+309F)
      - Katakana (U+30A0..U+30FF)
      - Hangul Syllables (U+AC00..U+D7AF)

    Ratio is ``count / len(text)`` (or 0.0 for empty text). The pair is
    returned so callers can apply both an absolute floor (8 chars) and a
    proportional floor (3%) — see `_CJK_REJECT_MIN_COUNT` /
    `_CJK_REJECT_MIN_RATIO`.
    """
    if not text:
        return 0.0, 0
    count = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3040 <= cp <= 0x309F
            or 0x30A0 <= cp <= 0x30FF
            or 0xAC00 <= cp <= 0xD7AF
        ):
            count += 1
    return count / len(text), count


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
