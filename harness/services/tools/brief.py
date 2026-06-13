"""save_business_brief — explicit brief-persistence tool.

The orchestrator's resume-fold step auto-persists a ``business_brief`` material
when the user approves ``request_approval(subject='business_brief')``. That
covers ONE approval path. In practice the agent often seeks brief sign-off via
``ask_user(question='Does this look good?', options=['Looks good!', ...])`` —
that path never triggers the auto-persist and the user's actual brief never
enters session memory.

This tool gives the agent a deterministic way to persist the brief from any
approval path. The agent is instructed (via the system prompt) to call it
AFTER the user has explicitly approved the brief, regardless of whether
approval came via ``request_approval`` or via ``ask_user`` with yes/no options.
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


def save_business_brief(args: dict, ctx: "ToolContext") -> "ToolResult":
    """Persist the brief as a business_brief material so downstream tools
    (render_mockup, validators, etc.) see the user's actual business info.

    Called by the agent AFTER the user has explicitly approved the brief.
    Idempotent in spirit — multiple persisted briefs are fine; tools use
    latest_material_by_type which returns the most recent.
    """
    from harness.services.tools import ToolResult

    brief = args.get("brief")
    if not isinstance(brief, dict):
        return _bad_args("'brief' must be a dict")
    if not brief:
        return _bad_args("'brief' must not be empty")

    material_id = store.persist_material(
        ctx.session_conn,
        direction=Direction.OUT.value,
        stage=ctx.stage,
        type=MaterialType.BUSINESS_BRIEF.value,
        content=brief,
        pending=False,
    )
    return ToolResult(
        ok=True,
        result={"material_id": material_id, "fields": list(brief.keys())},
    )
