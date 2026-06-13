"""render_mockup — deterministic ASCII wireframe from a layout_spec.

Pure function over args (apart from persisting the resulting mockup material
to satisfy the demo's checkpoint flow). Same input always produces identical
output bytes; the regions list mirrors the declared section names so the
mockup_renders checkpoint can verify coverage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.models.enums import Direction, MaterialType
from harness.services import store

if TYPE_CHECKING:
    from harness.services.tools import ToolContext, ToolResult


# Fixed canvas width keeps rendering deterministic and the demo readable.
_INNER_WIDTH = 30


def _bad_args(message: str) -> "ToolResult":
    from harness.services.tools import ToolResult

    return ToolResult(
        ok=False,
        error={"error_kind": "bad_args", "error_message": message},
    )


def _row(label: str) -> str:
    if len(label) > _INNER_WIDTH:
        label = label[:_INNER_WIDTH]
    pad = _INNER_WIDTH - len(label)
    left = pad // 2
    right = pad - left
    return "|" + (" " * left) + label + (" " * right) + "|"


def _border() -> str:
    return "+" + ("-" * _INNER_WIDTH) + "+"


def _render(sections: list[dict], primary_cta: str | None) -> str:
    lines: list[str] = [_border()]
    for section in sections:
        name = section["name"]
        lines.append(_row(name))
        lines.append(_border())
    if primary_cta:
        lines.append(_row(f"[{primary_cta}]"))
        lines.append(_border())
    return "\n".join(lines)


def render_mockup(args: dict, ctx: "ToolContext") -> "ToolResult":
    from harness.services.tools import ToolResult

    layout_spec = args.get("layout_spec")
    if not isinstance(layout_spec, dict):
        return _bad_args("'layout_spec' must be a dict")

    sections = layout_spec.get("sections")
    if not isinstance(sections, list) or not sections:
        return _bad_args("'layout_spec.sections' must be a non-empty list")

    cleaned: list[dict] = []
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            return _bad_args(f"section {idx} must be a dict")
        name = section.get("name")
        if not isinstance(name, str) or not name.strip():
            return _bad_args(f"section {idx} requires non-empty 'name'")
        cleaned.append(section)

    primary_cta = layout_spec.get("primary_cta")
    if primary_cta is not None and not isinstance(primary_cta, str):
        return _bad_args("'primary_cta' must be a string when provided")

    ascii_art = _render(cleaned, primary_cta)
    regions = [s["name"] for s in cleaned]

    material_id = store.persist_material(
        ctx.session_conn,
        direction=Direction.OUT.value,
        stage=ctx.stage,
        type=MaterialType.MOCKUP.value,
        content={"ascii": ascii_art, "regions": regions},
        pending=False,
    )

    return ToolResult(
        ok=True,
        result={
            "ascii": ascii_art,
            "regions": regions,
            "material_id": material_id,
        },
    )
