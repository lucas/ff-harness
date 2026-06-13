"""Worker contract: the `Worker` Protocol and a `MockWorker` for tests.

The Protocol is left structural (no `@runtime_checkable`) so we depend on
Pyright/duck-typing rather than isinstance. `LLMWorker` (Step 8) will be the
real implementation; `MockWorker` covers Step 7's orchestrator E2E test.
"""

from __future__ import annotations

from typing import Protocol

from harness.models.envelope import Escalate, Final, ToolCall, WorkerContext


class Worker(Protocol):
    def act(self, ctx: WorkerContext) -> ToolCall | Final | Escalate: ...


class MockWorkerExhausted(IndexError):
    """Raised when a MockWorker's scripted queue is empty."""


class MockWorker:
    """Pops scripted responses in FIFO order; records received contexts.

    Used by Step 7's orchestrator integration test and any dev iteration where
    burning real LLM quota would be wasteful.
    """

    def __init__(
        self, scripted_responses: list[ToolCall | Final | Escalate]
    ) -> None:
        # Copy the list so the caller can't mutate our queue out from under us.
        self._queue: list[ToolCall | Final | Escalate] = list(scripted_responses)
        self.received_contexts: list[WorkerContext] = []

    def act(self, ctx: WorkerContext) -> ToolCall | Final | Escalate:
        self.received_contexts.append(ctx)
        if not self._queue:
            raise MockWorkerExhausted(
                f"MockWorker queue exhausted at turn={ctx.turn} stage={ctx.stage!r};"
                f" received {len(self.received_contexts)} call(s)."
            )
        return self._queue.pop(0)
