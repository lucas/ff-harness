"""Step 2 gate — MockWorker FIFO behavior and structural Protocol conformance."""

from __future__ import annotations

import pytest

from harness.models.envelope import (
    Escalate,
    Final,
    Message,
    ToolCall,
    WorkerContext,
)
from harness.services.worker import MockWorker, MockWorkerExhausted, Worker


def _ctx(turn: int, stage: str = "bootstrap") -> WorkerContext:
    return WorkerContext(
        session_id="0190a8d4-9b1c-7c3e-9c4d-8f2e1a5b6c7d",
        turn=turn,
        stage=stage,
        system_prompt="sys",
        messages=[Message(role="system", content="hi")],
        tool_schemas=[],
        state={},
    )


def test_scripted_responses_pop_in_fifo_order():
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="ask_user", args={"question": "name?"}),
        ToolCall(tool="render_mockup", args={"layout_spec": {}}),
        Final(summary="done"),
    ]
    worker = MockWorker(scripted_responses=scripted)

    r1 = worker.act(_ctx(turn=1, stage="bootstrap"))
    r2 = worker.act(_ctx(turn=2, stage="mockup"))
    r3 = worker.act(_ctx(turn=3, stage="build"))

    assert isinstance(r1, ToolCall) and r1.tool == "ask_user"
    assert isinstance(r2, ToolCall) and r2.tool == "render_mockup"
    assert isinstance(r3, Final) and r3.summary == "done"


def test_received_contexts_accumulates_in_order():
    worker = MockWorker(scripted_responses=[Final(summary="a"), Final(summary="b")])
    worker.act(_ctx(turn=1, stage="bootstrap"))
    worker.act(_ctx(turn=2, stage="mockup"))

    assert len(worker.received_contexts) == 2
    assert worker.received_contexts[0].turn == 1
    assert worker.received_contexts[0].stage == "bootstrap"
    assert worker.received_contexts[1].turn == 2
    assert worker.received_contexts[1].stage == "mockup"


def test_empty_queue_raises_mock_worker_exhausted():
    worker = MockWorker(scripted_responses=[])
    with pytest.raises(MockWorkerExhausted):
        worker.act(_ctx(turn=1))


def test_exhaustion_after_drain_raises():
    worker = MockWorker(scripted_responses=[Final(summary="only one")])
    worker.act(_ctx(turn=1))
    with pytest.raises(MockWorkerExhausted):
        worker.act(_ctx(turn=2))


def test_mock_worker_exhausted_is_subclass_of_index_error():
    # Subclassing IndexError lets the spec's "raises IndexError" expectation
    # also hold without losing the more specific exception type.
    assert issubclass(MockWorkerExhausted, IndexError)


def test_mock_worker_satisfies_worker_protocol_structurally():
    worker = MockWorker(scripted_responses=[Final(summary="x")])
    # Worker is a pure structural Protocol (no runtime_checkable); assert by
    # attribute presence and callability rather than isinstance.
    assert hasattr(worker, "act")
    assert callable(worker.act)

    # Pyright-compat: assignment into a Worker-typed local binds without error.
    w: Worker = worker
    assert w is worker


def test_constructor_copies_input_list_so_caller_cannot_mutate_queue():
    scripted: list[ToolCall | Final | Escalate] = [Final(summary="first")]
    worker = MockWorker(scripted_responses=scripted)
    scripted.append(Final(summary="injected"))  # post-construction mutation
    # First call should still return the original single item, then exhaust.
    r = worker.act(_ctx(turn=1))
    assert isinstance(r, Final) and r.summary == "first"
    with pytest.raises(MockWorkerExhausted):
        worker.act(_ctx(turn=2))
