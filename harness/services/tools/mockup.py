"""render_mockup — deterministic ASCII wireframe + themed HTML preview.

Pure function over args (apart from persisting the resulting mockup material
to satisfy the demo's checkpoint flow). Same input always produces identical
ASCII bytes; the regions list mirrors the declared section names so the
mockup_renders checkpoint can verify coverage. The themed HTML is a
designer-wireframe view of the same layout, themed against the most recent
`business_brief` material when one is on file. The HTML is self-contained
(no external resources) and meant to be embedded as an iframe `srcdoc`
preview in the chat panel.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from harness.models.enums import Direction, MaterialType
from harness.services import store

if TYPE_CHECKING:
    from harness.services.tools import ToolContext, ToolResult


# Fixed canvas width keeps rendering deterministic and the demo readable.
_INNER_WIDTH = 30

# Hex color sanity check (mirrors view_helpers._HEX_COLOR_RE). Anything that
# doesn't match falls back to a neutral default — no inline-style poisoning.
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")

# Neutral palette used when no brief is present, or when the brief's palette
# entries don't pass the hex regex.
_NEUTRAL_PRIMARY = "#444"
_NEUTRAL_SECONDARY = "#eee"


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


# ---------------------------------------------------------------------------
# HTML wireframe generation
# ---------------------------------------------------------------------------


def _safe_hex(value: object, fallback: str) -> str:
    """Return ``value`` if it's a valid hex color string, else ``fallback``.

    Rejects anything that isn't a string matching ``^#[0-9A-Fa-f]{3,8}$`` —
    so `"javascript:alert(1)"` or other style-poisoning values can never
    reach the generated HTML's inline styles.
    """
    if isinstance(value, str) and _HEX_COLOR_RE.match(value):
        return value
    return fallback


def _classify(name: str) -> str:
    """Return a coarse role for a section name (header / hero / footer / other)."""
    lowered = name.strip().lower()
    if lowered in {"header", "nav", "navigation", "navbar"}:
        return "header"
    if lowered in {"hero", "banner", "splash"}:
        return "hero"
    if lowered == "footer":
        return "footer"
    return "other"


def _brief_field(brief: dict | None, key: str) -> str | None:
    """Pull a string field from the brief content dict, or None if absent/blank."""
    if not isinstance(brief, dict):
        return None
    value = brief.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _build_mockup_html(
    sections: list[dict],
    primary_cta: str | None,
    brief: dict | None,
) -> str:
    """Return a complete ``<!doctype html>`` document. Themed if brief present.

    The output is self-contained — no remote fonts, no external CSS, no JS,
    no images. Safe to drop into an ``<iframe sandbox srcdoc=...>`` preview.
    All brief-derived values are HTML-escaped before insertion. The primary
    CTA is sourced from the explicit ``primary_cta`` arg, falling back to
    ``brief['primary_cta']`` when the arg is None.
    """
    themed = isinstance(brief, dict) and bool(brief)

    palette: dict[str, object] = {}
    if themed and isinstance(brief, dict):
        raw_palette = brief.get("palette")
        if isinstance(raw_palette, dict):
            palette = raw_palette

    primary = _safe_hex(palette.get("primary"), _NEUTRAL_PRIMARY)
    secondary = _safe_hex(palette.get("secondary"), _NEUTRAL_SECONDARY)

    brand_name = _brief_field(brief, "name")
    tagline = _brief_field(brief, "tagline")

    cta = primary_cta if isinstance(primary_cta, str) and primary_cta else None
    if cta is None:
        cta = _brief_field(brief, "primary_cta")

    # Locate the first Header (if any) so we know whether to put the brand
    # name in a dedicated header band or to hoist it onto the first section.
    header_index: int | None = None
    for idx, sec in enumerate(sections):
        if _classify(sec["name"]) == "header":
            header_index = idx
            break

    style = (
        "<style>"
        "*{box-sizing:border-box;}"
        "html,body{margin:0;padding:0;}"
        "body{"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "'Helvetica Neue',Arial,sans-serif;"
        "color:#222;background:#fff;font-size:14px;line-height:1.4;"
        "}"
        ".mock-section{"
        "border:1px dashed #bbb;background:#fafafa;color:#555;"
        "padding:16px;margin:0;display:flex;align-items:center;"
        "justify-content:center;text-align:center;min-height:60px;"
        "}"
        ".mock-section + .mock-section{border-top:none;}"
        f".mock-header{{background:{html.escape(primary)};color:#fff;"
        "border:none;font-weight:600;letter-spacing:0.04em;}"
        f".mock-hero{{background:{html.escape(secondary)};color:#222;"
        "border:none;min-height:180px;flex-direction:column;gap:8px;}"
        ".mock-hero h1{margin:0;font-size:24px;font-weight:700;}"
        ".mock-hero .tagline{margin:0;font-style:italic;color:#555;}"
        f".mock-cta{{display:inline-block;margin-top:8px;"
        f"background:{html.escape(primary)};color:#fff;"
        "padding:8px 16px;border-radius:4px;font-weight:600;}"
        ".mock-footer{background:#222;color:#bbb;border:none;font-size:12px;}"
        ".mock-label{font-size:12px;color:#888;text-transform:uppercase;"
        "letter-spacing:0.06em;}"
        "</style>"
    )

    body_parts: list[str] = []
    for idx, sec in enumerate(sections):
        name = sec["name"]
        role = _classify(name)
        esc_name = html.escape(name)

        if role == "header":
            header_text = brand_name if brand_name else name
            body_parts.append(
                f'<section class="mock-section mock-header">'
                f"{html.escape(header_text)}"
                f"</section>"
            )
        elif role == "hero" or (header_index is None and idx == 0):
            # First non-header section (or anything explicitly hero/banner)
            # gets the marquee treatment: brand + tagline + CTA.
            heading_text = brand_name if brand_name else name
            inner: list[str] = [f"<h1>{html.escape(heading_text)}</h1>"]
            if tagline:
                inner.append(
                    f'<p class="tagline">{html.escape(tagline)}</p>'
                )
            if cta:
                inner.append(
                    f'<span class="mock-cta">{html.escape(cta)}</span>'
                )
            # If this section wasn't actually a hero/banner, surface its
            # name as a small label so the wireframe still reads correctly.
            if role != "hero":
                inner.insert(
                    0, f'<div class="mock-label">{esc_name}</div>'
                )
            body_parts.append(
                '<section class="mock-section mock-hero">'
                + "".join(inner)
                + "</section>"
            )
        elif role == "footer":
            body_parts.append(
                f'<section class="mock-section mock-footer">'
                f"{esc_name}"
                f"</section>"
            )
        else:
            body_parts.append(
                f'<section class="mock-section">{esc_name}</section>'
            )

    # If a primary CTA was supplied but nothing in the layout claimed it
    # (no hero, no first-section-hoisted), append a standalone CTA strip so
    # the wireframe still surfaces the action.
    cta_consumed = any(
        _classify(s["name"]) == "hero" or (header_index is None and i == 0)
        for i, s in enumerate(sections)
    )
    if cta and not cta_consumed:
        body_parts.append(
            '<section class="mock-section">'
            f'<span class="mock-cta">{html.escape(cta)}</span>'
            "</section>"
        )

    title_text = brand_name if brand_name else "Mockup preview"
    return (
        "<!doctype html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title_text)}</title>"
        f"{style}"
        "</head>"
        "<body>"
        + "".join(body_parts)
        + "</body></html>"
    )


def _latest_business_brief(session_conn) -> dict | None:
    """Return the most recent ``business_brief`` material content dict, or None."""
    row = store.latest_material_by_type(
        session_conn, MaterialType.BUSINESS_BRIEF.value
    )
    if row is None:
        return None
    content = row.get("content")
    return content if isinstance(content, dict) else None


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

    brief = _latest_business_brief(ctx.session_conn)
    themed = brief is not None and bool(brief)
    html_doc = _build_mockup_html(cleaned, primary_cta, brief)

    material_id = store.persist_material(
        ctx.session_conn,
        direction=Direction.OUT.value,
        stage=ctx.stage,
        type=MaterialType.MOCKUP.value,
        content={
            "ascii": ascii_art,
            "regions": regions,
            "html": html_doc,
            "themed": themed,
        },
        pending=False,
    )

    return ToolResult(
        ok=True,
        result={
            "ascii": ascii_art,
            "regions": regions,
            "html": html_doc,
            "themed": themed,
            "material_id": material_id,
        },
    )
