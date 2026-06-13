"""Tool dispatcher + ToolContext / ToolResult dataclasses.

This module is the single entry point for worker-callable tools. It enforces
two pre-dispatch gates (allow-list, sandbox-path for file tools) before
delegating to the implementation in REGISTRY, and converts any exception that
escapes a tool function into a fail-as-data ToolResult plus a tool_failed alarm.

Layer rule: imports stdlib + harness.models.* + harness.services.{store,
guardrails, alarms} only. Never from orchestrator / llm / api.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness.services import guardrails
from harness.services.alarms import raise_tool_failed
from harness.services.tools import files, mockup, user


@dataclass
class ToolContext:
    """Per-call state passed to every tool function.

    session_conn — per-session sqlite connection (for material/event/alarm writes)
    sandbox_path — absolute root under which file tools may operate
    stage        — current stage string (used in alarm rows + material rows)
    allow_list   — tool names the worker is permitted to invoke this turn
    """

    session_conn: sqlite3.Connection
    sandbox_path: Path
    stage: str
    allow_list: list[str]


@dataclass
class ToolResult:
    """Tool return value. Errors are data, not exceptions.

    pause_reason is set to 'awaiting_human' iff the tool wants the orchestrator
    to exit run_until_pause cleanly (ask_user / request_approval).
    """

    ok: bool
    result: dict | None = None
    error: dict | None = None
    pause_reason: str | None = None


# Tool names whose args dict must carry a sandboxed path. Used for the
# dispatcher-level path-safety pre-check.
_FILE_TOOLS = frozenset({"read_file", "write_file", "list_files"})


REGISTRY: dict[str, Callable[[dict, "ToolContext"], "ToolResult"]] = {
    "ask_user": user.ask_user,
    "request_approval": user.request_approval,
    "read_file": files.read_file,
    "write_file": files.write_file,
    "list_files": files.list_files,
    "render_mockup": mockup.render_mockup,
}


def _fail(error_kind: str, error_message: str) -> ToolResult:
    return ToolResult(
        ok=False,
        error={"error_kind": error_kind, "error_message": error_message},
    )


def dispatch(name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """Single entry point for worker-callable tools.

    Order of checks:
      1. allow-list (guardrails.is_tool_allowed)
      2. registry lookup (programmer-error guard: allowed but unimplemented)
      3. sandbox pre-check for file tools (guardrails.is_path_safe)
      4. invoke REGISTRY[name](args, ctx) inside a try/except

    Any check failure raises a tool_failed alarm and returns ToolResult(ok=False).
    Any exception escaping the tool function is caught here, converted to a
    tool_exception ToolResult, and likewise alarmed. Exceptions never propagate
    out of dispatch.
    """
    if not guardrails.is_tool_allowed(name, ctx.allow_list):
        msg = f"tool {name} not in allow-list"
        raise_tool_failed(
            ctx.session_conn,
            tool=name,
            args=args,
            error_kind="denied_by_allowlist",
            error_message=msg,
            stage=ctx.stage,
        )
        return _fail("denied_by_allowlist", msg)

    impl = REGISTRY.get(name)
    if impl is None:
        msg = f"tool {name} has no implementation in REGISTRY"
        raise_tool_failed(
            ctx.session_conn,
            tool=name,
            args=args,
            error_kind="not_implemented",
            error_message=msg,
            stage=ctx.stage,
        )
        return _fail("not_implemented", msg)

    if name in _FILE_TOOLS:
        path_arg = args.get("path")
        # list_files has an optional path arg; only enforce when present.
        if path_arg is not None:
            absolute = (Path(ctx.sandbox_path) / path_arg).resolve(strict=False)
            if not guardrails.is_path_safe(absolute, ctx.sandbox_path):
                msg = f"path {path_arg!r} resolves outside sandbox"
                raise_tool_failed(
                    ctx.session_conn,
                    tool=name,
                    args=args,
                    error_kind="path_escape",
                    error_message=msg,
                    stage=ctx.stage,
                )
                return _fail("path_escape", msg)

    try:
        return impl(args, ctx)
    except Exception as exc:
        msg = str(exc)
        raise_tool_failed(
            ctx.session_conn,
            tool=name,
            args=args,
            error_kind="tool_exception",
            error_message=msg,
            stage=ctx.stage,
        )
        return _fail("tool_exception", msg)


__all__ = ["ToolContext", "ToolResult", "REGISTRY", "dispatch"]
