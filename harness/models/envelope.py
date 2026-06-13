"""Pydantic envelope for worker I/O.

These are the only shapes the orchestrator will accept from a worker. The
discriminated union on `type` lets a single parse call route to the correct
variant; callers handle `pydantic.ValidationError` (typically by raising an
`output_schema_violation` alarm).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, PositiveInt, TypeAdapter


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None


class WorkerContext(BaseModel):
    session_id: str
    turn: PositiveInt
    stage: str
    system_prompt: str
    messages: list[Message]
    tool_schemas: list[dict]
    state: dict


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    tool: str
    args: dict


class Final(BaseModel):
    type: Literal["final"] = "final"
    summary: str


class Escalate(BaseModel):
    type: Literal["escalate"] = "escalate"
    reason: str


WorkerResponse = Annotated[
    Union[ToolCall, Final, Escalate], Field(discriminator="type")
]


# Module-level TypeAdapter so we pay Pydantic's schema-build cost once.
_WORKER_RESPONSE_ADAPTER: TypeAdapter[Union[ToolCall, Final, Escalate]] = TypeAdapter(
    WorkerResponse
)


def parse_worker_response(raw: str | dict) -> ToolCall | Final | Escalate:
    """Parse a worker's raw output into the discriminated union.

    Accepts either a JSON string or an already-decoded dict. Raises
    `pydantic.ValidationError` on schema violation (including malformed JSON,
    unknown `type` discriminator, or missing required fields). Callers are
    expected to convert this into an `output_schema_violation` alarm.
    """
    if isinstance(raw, str):
        return _WORKER_RESPONSE_ADAPTER.validate_json(raw)
    return _WORKER_RESPONSE_ADAPTER.validate_python(raw)
