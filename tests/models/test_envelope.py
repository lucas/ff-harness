"""Step 2 gate — Pydantic envelope shapes and discriminated-union parsing."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from harness.models.envelope import (
    Escalate,
    Final,
    Message,
    ToolCall,
    WorkerContext,
    parse_worker_response,
)


# ---------------------------------------------------------------------------
# parse_worker_response: round-trip from dict + JSON for each variant
# ---------------------------------------------------------------------------


def test_parse_tool_call_from_dict():
    parsed = parse_worker_response(
        {"type": "tool_call", "tool": "write_file", "args": {"path": "index.html"}}
    )
    assert isinstance(parsed, ToolCall)
    assert parsed.tool == "write_file"
    assert parsed.args == {"path": "index.html"}


def test_parse_tool_call_from_json_string():
    raw = json.dumps(
        {"type": "tool_call", "tool": "ask_user", "args": {"question": "name?"}}
    )
    parsed = parse_worker_response(raw)
    assert isinstance(parsed, ToolCall)
    assert parsed.tool == "ask_user"


def test_parse_final_from_dict():
    parsed = parse_worker_response({"type": "final", "summary": "all done"})
    assert isinstance(parsed, Final)
    assert parsed.summary == "all done"


def test_parse_final_from_json_string():
    parsed = parse_worker_response('{"type":"final","summary":"shipped"}')
    assert isinstance(parsed, Final)
    assert parsed.summary == "shipped"


def test_parse_escalate_from_dict():
    parsed = parse_worker_response({"type": "escalate", "reason": "stuck on css"})
    assert isinstance(parsed, Escalate)
    assert parsed.reason == "stuck on css"


def test_parse_escalate_from_json_string():
    parsed = parse_worker_response('{"type":"escalate","reason":"need help"}')
    assert isinstance(parsed, Escalate)
    assert parsed.reason == "need help"


# ---------------------------------------------------------------------------
# parse_worker_response: rejects bad input
# ---------------------------------------------------------------------------


def test_parse_unknown_discriminator_raises():
    with pytest.raises(ValidationError):
        parse_worker_response({"type": "wat", "summary": "x"})


def test_parse_malformed_json_raises():
    with pytest.raises(ValidationError):
        parse_worker_response("{not valid json")


def test_parse_missing_discriminator_raises():
    with pytest.raises(ValidationError):
        parse_worker_response({"tool": "write_file", "args": {}})


# ---------------------------------------------------------------------------
# Per-variant required field enforcement
# ---------------------------------------------------------------------------


def test_tool_call_requires_tool_and_args():
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"type": "tool_call", "args": {}})
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"type": "tool_call", "tool": "write_file"})


def test_final_requires_summary():
    with pytest.raises(ValidationError):
        Final.model_validate({"type": "final"})


def test_escalate_requires_reason():
    with pytest.raises(ValidationError):
        Escalate.model_validate({"type": "escalate"})


# ---------------------------------------------------------------------------
# WorkerContext field validation
# ---------------------------------------------------------------------------


def _valid_ctx_kwargs(**overrides) -> dict:
    base = dict(
        session_id="0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d",
        turn=1,
        stage="bootstrap",
        system_prompt="you are a website builder",
        messages=[Message(role="system", content="hi")],
        tool_schemas=[{"name": "ask_user"}],
        state={"sandbox_path": "/tmp/sandbox"},
    )
    base.update(overrides)
    return base


def test_worker_context_accepts_minimal_valid_input():
    ctx = WorkerContext(**_valid_ctx_kwargs())
    assert ctx.turn == 1
    assert ctx.stage == "bootstrap"
    assert ctx.session_id.startswith("0190")


def test_worker_context_rejects_zero_turn():
    with pytest.raises(ValidationError):
        WorkerContext(**_valid_ctx_kwargs(turn=0))


def test_worker_context_rejects_negative_turn():
    with pytest.raises(ValidationError):
        WorkerContext(**_valid_ctx_kwargs(turn=-3))


def test_worker_context_rejects_non_int_turn():
    with pytest.raises(ValidationError):
        WorkerContext(**_valid_ctx_kwargs(turn="one"))


def test_worker_context_requires_all_fields():
    for field in (
        "session_id",
        "turn",
        "stage",
        "system_prompt",
        "messages",
        "tool_schemas",
        "state",
    ):
        kwargs = _valid_ctx_kwargs()
        kwargs.pop(field)
        with pytest.raises(ValidationError):
            WorkerContext(**kwargs)


def test_worker_context_accepts_all_four_message_roles():
    msgs = [
        Message(role="system", content="s"),
        Message(role="user", content="u"),
        Message(role="assistant", content="a"),
        Message(role="tool", content="t", tool_call_id="call_1"),
    ]
    ctx = WorkerContext(**_valid_ctx_kwargs(messages=msgs))
    assert [m.role for m in ctx.messages] == ["system", "user", "assistant", "tool"]


def test_worker_context_rejects_unknown_message_role():
    with pytest.raises(ValidationError):
        WorkerContext(
            **_valid_ctx_kwargs(
                messages=[{"role": "robot", "content": "beep"}],
            )
        )


# ---------------------------------------------------------------------------
# Message defaults
# ---------------------------------------------------------------------------


def test_tool_message_can_set_tool_call_id():
    m = Message(role="tool", content="result", tool_call_id="call_abc")
    assert m.tool_call_id == "call_abc"


def test_non_tool_messages_default_tool_call_id_to_none():
    for role in ("system", "user", "assistant"):
        m = Message(role=role, content="x")
        assert m.tool_call_id is None
