"""Step 8 gate — LLMWorker offline coverage + one opt-in live smoke test.

Offline tests (always run) use a hand-rolled `StubLLMClient` injected into
`LLMWorker`; nothing touches the real OpenRouter API. The live test is
gated by the `live` pytest marker and is skipped unless
`OPENROUTER_API_KEY` is in the environment.

Spend-log policy under test (matches `llm_worker.py` docstring):
  - One row per SUCCESSFUL OpenRouter call.
  - 429s and transport failures write NO spend_log row.
  - Repair retry adds a second row when (and only when) the retry call
    itself succeeds at the transport layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from harness.models.enums import AlarmType, EventType
from harness.models.envelope import (
    Escalate,
    Final,
    Message,
    ToolCall,
    WorkerContext,
)
from harness.services import store
from harness.services.llm import (
    ChatResponse,
    LLMTransportError,
    OpenRouterClient,
    RateLimited,
)
from harness.services.llm_worker import LLMWorker


# ---------------------------------------------------------------------------
# Fixtures + stub client
# ---------------------------------------------------------------------------


_PRIMARY = "deepseek/deepseek-v4-flash:free"
_FALLBACK = "deepseek/deepseek-v4-flash"


class StubLLMClient:
    """Hand-rolled fake OpenRouter client.

    Pops a scripted queue per `chat()` call. Each item is either a
    `ChatResponse` to return or an `Exception` to raise. Recorded calls
    live on `.calls` so tests can assert ordering, model, and arguments.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses: list[Any] = list(responses)
        self.calls: list[dict] = []

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0.2,
    ) -> ChatResponse:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_format": response_format,
                "temperature": temperature,
            }
        )
        if not self.responses:
            raise AssertionError(
                f"StubLLMClient queue empty at call #{len(self.calls)};"
                f" model={model!r}"
            )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ChatResponse):
            return item
        raise AssertionError(
            f"StubLLMClient queue item must be ChatResponse or Exception,"
            f" got {type(item).__name__}"
        )


def _chat_response(text: str, *, cost: float = 0.0) -> ChatResponse:
    return ChatResponse(
        text=text,
        tokens_in=10,
        tokens_out=20,
        cost_usd=cost,
        model_used="ignored-in-tests",
    )


def _valid_toolcall_json() -> str:
    return json.dumps(
        {"type": "tool_call", "tool": "ask_user", "args": {"question": "hi?"}}
    )


def _valid_final_json() -> str:
    return json.dumps({"type": "final", "summary": "done"})


def _valid_escalate_json() -> str:
    return json.dumps({"type": "escalate", "reason": "human help"})


def _make_ctx(
    session_id: str,
    *,
    stage: str = "bootstrap",
    turn: int = 1,
) -> WorkerContext:
    return WorkerContext(
        session_id=session_id,
        turn=turn,
        stage=stage,
        system_prompt="you are a test worker. reply only with the envelope.",
        messages=[
            Message(role="system", content="system"),
            Message(role="user", content="hello"),
        ],
        tool_schemas=[],
        state={},
    )


def _build_worker(
    *,
    tmp_path: Path,
    stub: StubLLMClient,
    fallback: str | None = _FALLBACK,
) -> tuple[LLMWorker, sqlite3.Connection, sqlite3.Connection, str]:
    """Wire up a worker with fresh tmp DBs and return everything for asserts."""
    core_conn = store.core_connection(tmp_path / "harness.db")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = store.create_session(core_conn)
    session_conn = store.session_connection(sessions_dir, session_id)
    worker = LLMWorker(
        primary=_PRIMARY,
        fallback=fallback,
        llm_client=stub,  # type: ignore[arg-type]
        core_conn=core_conn,
        session_conn=session_conn,
        session_id=session_id,
    )
    return worker, core_conn, session_conn, session_id


def _load_spend_rows(core_conn: sqlite3.Connection) -> list[dict]:
    rows = core_conn.execute(
        "SELECT id, ts, session_id, model, is_fallback, tokens_in, tokens_out,"
        " cost_usd FROM spend_log ORDER BY id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _model_swapped_events(session_conn: sqlite3.Connection) -> list[dict]:
    return [
        e
        for e in store.load_events(session_conn)
        if e["type"] == EventType.MODEL_SWAPPED.value
    ]


# ---------------------------------------------------------------------------
# 1. Happy-path scenarios
# ---------------------------------------------------------------------------


def test_happy_path_primary_toolcall(tmp_path: Path) -> None:
    stub = StubLLMClient([_chat_response(_valid_toolcall_json())])
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))

        assert isinstance(result, ToolCall)
        assert result.tool == "ask_user"
        assert result.args == {"question": "hi?"}

        spend = _load_spend_rows(core_conn)
        assert len(spend) == 1
        assert spend[0]["model"] == _PRIMARY
        assert spend[0]["is_fallback"] == 0

        assert _model_swapped_events(session_conn) == []
        assert len(stub.calls) == 1
        assert stub.calls[0]["model"] == _PRIMARY
        # JSON-mode must be requested on every call.
        assert stub.calls[0]["response_format"] == {"type": "json_object"}
    finally:
        session_conn.close()
        core_conn.close()


def test_happy_path_final(tmp_path: Path) -> None:
    stub = StubLLMClient([_chat_response(_valid_final_json())])
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))
        assert isinstance(result, Final)
        assert result.summary == "done"
        assert len(_load_spend_rows(core_conn)) == 1
    finally:
        session_conn.close()
        core_conn.close()


def test_happy_path_escalate(tmp_path: Path) -> None:
    stub = StubLLMClient([_chat_response(_valid_escalate_json())])
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))
        assert isinstance(result, Escalate)
        assert result.reason == "human help"
        assert len(_load_spend_rows(core_conn)) == 1
    finally:
        session_conn.close()
        core_conn.close()


# ---------------------------------------------------------------------------
# 2. Repair retry on bad JSON
# ---------------------------------------------------------------------------


def test_repair_retry_succeeds_on_primary(tmp_path: Path) -> None:
    stub = StubLLMClient(
        [
            _chat_response("not json"),
            _chat_response(_valid_toolcall_json()),
        ]
    )
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))

        assert isinstance(result, ToolCall)
        assert result.tool == "ask_user"

        # Both calls succeeded transport-side → both produce spend rows.
        spend = _load_spend_rows(core_conn)
        assert len(spend) == 2
        assert all(row["model"] == _PRIMARY for row in spend)
        assert all(row["is_fallback"] == 0 for row in spend)

        # No alarms raised on a successful repair.
        alarms = store.load_alarms(session_conn)
        assert alarms == []

        # Second call must include the repair message appended.
        assert len(stub.calls) == 2
        repair_messages = stub.calls[1]["messages"]
        assert any(
            "valid JSON" in m["content"] and m["role"] == "user"
            for m in repair_messages
        )
    finally:
        session_conn.close()
        core_conn.close()


def test_repair_retry_fails_raises_schema_alarm(tmp_path: Path) -> None:
    stub = StubLLMClient(
        [
            _chat_response("not json"),
            _chat_response("still not json"),
        ]
    )
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))

        assert isinstance(result, Escalate)
        assert result.reason == "schema"

        # Both calls succeeded transport-side → both logged.
        assert len(_load_spend_rows(core_conn)) == 2

        alarms_rows = store.load_alarms(session_conn)
        assert len(alarms_rows) == 1
        alarm = alarms_rows[0]
        assert alarm["type"] == AlarmType.OUTPUT_SCHEMA_VIOLATION.value
        assert alarm["context"]["repair_attempt"] == 1
        assert "still not json" in alarm["context"]["raw_text_preview"]
    finally:
        session_conn.close()
        core_conn.close()


# ---------------------------------------------------------------------------
# 3. 429 / fallback behavior
# ---------------------------------------------------------------------------


def test_429_then_fallback_success(tmp_path: Path) -> None:
    stub = StubLLMClient(
        [
            RateLimited(_PRIMARY),
            _chat_response(_valid_toolcall_json()),
        ]
    )
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))

        assert isinstance(result, ToolCall)

        # Spend-log choice (documented in llm_worker.py): do NOT log a row
        # for the failed primary 429 — only the successful fallback gets a
        # row, with is_fallback=1.
        spend = _load_spend_rows(core_conn)
        assert len(spend) == 1
        assert spend[0]["model"] == _FALLBACK
        assert spend[0]["is_fallback"] == 1

        swaps = _model_swapped_events(session_conn)
        assert len(swaps) == 1
        assert swaps[0]["payload"] == {
            "from": _PRIMARY,
            "to": _FALLBACK,
            "reason": "rate_limited",
        }

        # No alarms on a successful swap.
        assert store.load_alarms(session_conn) == []

        assert [c["model"] for c in stub.calls] == [_PRIMARY, _FALLBACK]
    finally:
        session_conn.close()
        core_conn.close()


def test_429_then_fallback_also_429(tmp_path: Path) -> None:
    stub = StubLLMClient(
        [
            RateLimited(_PRIMARY),
            RateLimited(_FALLBACK),
        ]
    )
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))

        assert isinstance(result, Escalate)
        assert "rate" in result.reason.lower()

        # Both calls failed → no spend rows.
        assert _load_spend_rows(core_conn) == []

        # The swap event still fires before the fallback attempt.
        swaps = _model_swapped_events(session_conn)
        assert len(swaps) == 1
        assert swaps[0]["payload"]["to"] == _FALLBACK

        alarms_rows = store.load_alarms(session_conn)
        assert len(alarms_rows) == 1
        alarm = alarms_rows[0]
        assert alarm["type"] == AlarmType.TOOL_FAILED.value
        assert alarm["context"]["error_kind"] == "rate_limited"
        assert alarm["context"]["args"] == {"model": _FALLBACK}
    finally:
        session_conn.close()
        core_conn.close()


def test_429_with_no_fallback_configured(tmp_path: Path) -> None:
    stub = StubLLMClient([RateLimited(_PRIMARY)])
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub, fallback=None
    )
    try:
        result = worker.act(_make_ctx("sid"))

        assert isinstance(result, Escalate)
        assert "rate" in result.reason.lower()

        # No fallback → no second call, no spend, no swap event.
        assert _load_spend_rows(core_conn) == []
        assert _model_swapped_events(session_conn) == []
        assert len(stub.calls) == 1

        alarms_rows = store.load_alarms(session_conn)
        assert len(alarms_rows) == 1
        alarm = alarms_rows[0]
        assert alarm["type"] == AlarmType.TOOL_FAILED.value
        assert alarm["context"]["error_kind"] == "rate_limited"
        assert alarm["context"]["args"] == {"model": _PRIMARY}
    finally:
        session_conn.close()
        core_conn.close()


def test_429_treats_empty_string_fallback_as_none(tmp_path: Path) -> None:
    """`MODEL_CODE_FALLBACK=""` from .env should behave like an unset fallback."""
    stub = StubLLMClient([RateLimited(_PRIMARY)])
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub, fallback=""
    )
    try:
        result = worker.act(_make_ctx("sid"))
        assert isinstance(result, Escalate)
        assert _model_swapped_events(session_conn) == []
        assert len(stub.calls) == 1
    finally:
        session_conn.close()
        core_conn.close()


# ---------------------------------------------------------------------------
# 4. Transport failure
# ---------------------------------------------------------------------------


def test_transport_error_raises_tool_failed_alarm(tmp_path: Path) -> None:
    stub = StubLLMClient([LLMTransportError("connection reset")])
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        result = worker.act(_make_ctx("sid"))

        assert isinstance(result, Escalate)
        assert "transport" in result.reason.lower()

        assert _load_spend_rows(core_conn) == []
        assert _model_swapped_events(session_conn) == []

        alarms_rows = store.load_alarms(session_conn)
        assert len(alarms_rows) == 1
        alarm = alarms_rows[0]
        assert alarm["type"] == AlarmType.TOOL_FAILED.value
        assert alarm["context"]["error_kind"] == "transport"
        assert "connection reset" in alarm["context"]["error_message"]
    finally:
        session_conn.close()
        core_conn.close()


# ---------------------------------------------------------------------------
# 5. Combined: 429-then-fallback-success, then a second act() with
#    fallback's first response being bad JSON; repair on the (now-known-
#    sticky) fallback model must succeed and log both rows with is_fallback=1.
# ---------------------------------------------------------------------------


def test_repair_works_on_fallback_after_prior_swap(tmp_path: Path) -> None:
    stub = StubLLMClient(
        [
            # First act(): primary 429, fallback success.
            RateLimited(_PRIMARY),
            _chat_response(_valid_toolcall_json()),
            # Second act(): primary 429 again, fallback bad-JSON, repair good.
            RateLimited(_PRIMARY),
            _chat_response("not json"),
            _chat_response(_valid_final_json()),
        ]
    )
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        first = worker.act(_make_ctx("sid", turn=1))
        assert isinstance(first, ToolCall)

        second = worker.act(_make_ctx("sid", turn=2))
        assert isinstance(second, Final)

        # Spend rows: 1 for first fallback success, 2 for the second
        # act()'s (fallback bad-JSON + fallback repair success). All three
        # are tagged is_fallback=1.
        spend = _load_spend_rows(core_conn)
        assert len(spend) == 3
        assert all(row["model"] == _FALLBACK for row in spend)
        assert all(row["is_fallback"] == 1 for row in spend)

        # Two swap events, one per act().
        swaps = _model_swapped_events(session_conn)
        assert len(swaps) == 2

        # No alarms — the repair on the fallback model worked.
        assert store.load_alarms(session_conn) == []
    finally:
        session_conn.close()
        core_conn.close()


# ---------------------------------------------------------------------------
# 6. Worker Protocol compliance (structural — no isinstance)
# ---------------------------------------------------------------------------


def test_llmworker_implements_worker_protocol(tmp_path: Path) -> None:
    """LLMWorker satisfies the `Worker` Protocol's `act` shape."""
    from harness.services.worker import Worker

    stub = StubLLMClient([_chat_response(_valid_final_json())])
    worker, core_conn, session_conn, _ = _build_worker(
        tmp_path=tmp_path, stub=stub
    )
    try:
        # Structural check: assignment satisfies the Protocol.
        as_worker: Worker = worker
        result = as_worker.act(_make_ctx("sid"))
        assert isinstance(result, (ToolCall, Final, Escalate))
    finally:
        session_conn.close()
        core_conn.close()


# ---------------------------------------------------------------------------
# Live test — opt-in only, gated by -m live + OPENROUTER_API_KEY.
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_chat_model_returns_final(tmp_path: Path) -> None:
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set; skipping live test")

    # Only the free chat model — guard the 200/day Qwen budget.
    primary = os.environ.get("MODEL_CHAT", "deepseek/deepseek-v4-flash:free")
    client = OpenRouterClient()
    core_conn = store.core_connection(tmp_path / "harness.db")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = store.create_session(core_conn)
    session_conn = store.session_connection(sessions_dir, session_id)

    worker = LLMWorker(
        primary=primary,
        fallback=None,
        llm_client=client,
        core_conn=core_conn,
        session_conn=session_conn,
        session_id=session_id,
    )
    try:
        ctx = WorkerContext(
            session_id=session_id,
            turn=1,
            stage="bootstrap",
            system_prompt=(
                "You are a test. Reply with exactly this JSON object and"
                " nothing else: {\"type\":\"final\",\"summary\":\"ok\"}"
            ),
            messages=[
                Message(
                    role="system",
                    content=(
                        "You are a test. Reply with exactly this JSON object"
                        " and nothing else:"
                        ' {"type":"final","summary":"ok"}'
                    ),
                ),
                Message(role="user", content="Go."),
            ],
            tool_schemas=[],
            state={},
        )
        result = worker.act(ctx)

        assert isinstance(result, Final)
        assert "ok" in result.summary.lower().strip()

        spend = _load_spend_rows(core_conn)
        assert len(spend) == 1
        assert spend[0]["cost_usd"] >= 0.0
    finally:
        session_conn.close()
        core_conn.close()
