"""Sandboxed file tools: read_file, write_file, list_files.

Every tool re-checks is_path_safe even though the dispatcher pre-checks too —
defense-in-depth so a future direct caller (or a wrong allow-list config)
still cannot escape the sandbox. write_file persists a site_file material;
post-hook chain wiring is the orchestrator's job (Step 7), not this layer's.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from harness.models.enums import Direction, MaterialType
from harness.services import guardrails, store

if TYPE_CHECKING:
    from harness.services.tools import ToolContext, ToolResult


_LIST_CAP = 200


def _bad_args(message: str) -> "ToolResult":
    from harness.services.tools import ToolResult

    return ToolResult(
        ok=False,
        error={"error_kind": "bad_args", "error_message": message},
    )


def _path_escape(path: str) -> "ToolResult":
    from harness.services.tools import ToolResult

    return ToolResult(
        ok=False,
        error={
            "error_kind": "path_escape",
            "error_message": f"path {path!r} resolves outside sandbox",
        },
    )


def _resolve_under_sandbox(path: str, sandbox: Path) -> Path:
    """Join a (relative or absolute) path to the sandbox without resolving symlinks of children."""
    p = Path(path)
    if p.is_absolute():
        return p.resolve(strict=False)
    return (sandbox / p).resolve(strict=False)


def read_file(args: dict, ctx: "ToolContext") -> "ToolResult":
    from harness.services.tools import ToolResult

    path = args.get("path")
    if not isinstance(path, str) or not path:
        return _bad_args("'path' must be a non-empty string")

    absolute = _resolve_under_sandbox(path, ctx.sandbox_path)
    if not guardrails.is_path_safe(absolute, ctx.sandbox_path):
        return _path_escape(path)

    if not absolute.exists() or not absolute.is_file():
        return ToolResult(
            ok=False,
            error={
                "error_kind": "not_found",
                "error_message": f"no file at {path!r}",
            },
        )

    content = absolute.read_text(encoding="utf-8")
    return ToolResult(
        ok=True,
        result={
            "path": path,
            "content": content,
            "bytes": len(content.encode("utf-8")),
        },
    )


def write_file(args: dict, ctx: "ToolContext") -> "ToolResult":
    from harness.services.tools import ToolResult

    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str) or not path:
        return _bad_args("'path' must be a non-empty string")
    if not isinstance(content, str):
        return _bad_args("'content' must be a string")

    absolute = _resolve_under_sandbox(path, ctx.sandbox_path)
    if not guardrails.is_path_safe(absolute, ctx.sandbox_path):
        return _path_escape(path)

    absolute.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    absolute.write_text(content, encoding="utf-8")

    content_hash = hashlib.sha256(encoded).hexdigest()[:16]
    material_id = store.persist_material(
        ctx.session_conn,
        direction=Direction.OUT.value,
        stage=ctx.stage,
        type=MaterialType.SITE_FILE.value,
        content={
            "path": path,
            "content_hash": content_hash,
            "bytes": len(encoded),
        },
        pending=False,
    )

    return ToolResult(
        ok=True,
        result={
            "path": path,
            "bytes": len(encoded),
            "material_id": material_id,
        },
    )


def list_files(args: dict, ctx: "ToolContext") -> "ToolResult":
    from harness.services.tools import ToolResult

    raw = args.get("path", ".")
    if not isinstance(raw, str):
        return _bad_args("'path' must be a string when provided")

    absolute = _resolve_under_sandbox(raw, ctx.sandbox_path)
    if not guardrails.is_path_safe(absolute, ctx.sandbox_path):
        return _path_escape(raw)

    if not absolute.exists() or not absolute.is_dir():
        return ToolResult(
            ok=False,
            error={
                "error_kind": "not_found",
                "error_message": f"no directory at {raw!r}",
            },
        )

    sandbox_resolved = Path(ctx.sandbox_path).resolve(strict=False)
    entries: list[str] = []
    truncated = False
    cap = _list_cap()
    for candidate in sorted(absolute.rglob("*")):
        if not candidate.is_file():
            continue
        try:
            rel = candidate.resolve(strict=False).relative_to(sandbox_resolved)
        except ValueError:
            # Skip anything that slipped outside the sandbox (symlinks, etc.).
            continue
        entries.append(str(rel))
        if len(entries) >= cap:
            truncated = True
            break

    return ToolResult(
        ok=True,
        result={"files": entries, "truncated": truncated},
    )


def _list_cap() -> int:
    """Indirection so tests can monkeypatch the cap cleanly."""
    return _LIST_CAP
