"""HITL escalation tools: ask_user, request_approval.

Both tools persist a pending_question material (pending=1) and return a
ToolResult with pause_reason='awaiting_human' so the orchestrator can exit
run_until_pause cleanly. The orchestrator is responsible for flipping session
status to awaiting_human; this layer just signals intent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.models.enums import Direction, MaterialType
from harness.services import store

if TYPE_CHECKING:
    from harness.services.tools import ToolContext, ToolResult


def _bad_args(message: str) -> "ToolResult":
    # Imported lazily to avoid the circular import with the dispatcher module.
    from harness.services.tools import ToolResult

    return ToolResult(
        ok=False,
        error={"error_kind": "bad_args", "error_message": message},
    )


def ask_user(args: dict, ctx: "ToolContext") -> "ToolResult":
    """Persist an open question for the human; pause the loop."""
    from harness.services.tools import ToolResult

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return _bad_args("'question' must be a non-empty string")

    material_id = store.persist_material(
        ctx.session_conn,
        direction=Direction.OUT.value,
        stage=ctx.stage,
        type=MaterialType.PENDING_QUESTION.value,
        content={"question": question, "options": args.get("options")},
        pending=True,
    )
    return ToolResult(
        ok=True,
        result={"material_id": material_id},
        pause_reason="awaiting_human",
    )


def request_approval(args: dict, ctx: "ToolContext") -> "ToolResult":
    """Persist an approval request for the human; pause the loop."""
    from harness.services.tools import ToolResult

    subject = args.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        return _bad_args("'subject' must be a non-empty string")

    details = args.get("details")
    if details is not None and not isinstance(details, dict):
        return _bad_args("'details' must be a dict when provided")

    material_id = store.persist_material(
        ctx.session_conn,
        direction=Direction.OUT.value,
        stage=ctx.stage,
        type=MaterialType.PENDING_QUESTION.value,
        content={"kind": "approval", "subject": subject, "details": details},
        pending=True,
    )
    return ToolResult(
        ok=True,
        result={"material_id": material_id},
        pause_reason="awaiting_human",
    )
