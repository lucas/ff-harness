"""render_mockup — deterministic ASCII wireframe + themed HTML preview.

The HTML output has two production paths:

1. **LLM path** (preferred when ``ctx.code_chat`` is provided): a closure
   bound to the code worker's model (e.g. ``qwen3-coder:free``) is asked to
   generate a self-contained ``<!doctype html>`` wireframe themed against
   the latest ``business_brief`` material. The returned text is
   sanity-validated (must start with ``<!doctype`` or ``<html``; must NOT
   contain ``<script``). Every attempt — success or failure — is recorded
   to the per-session ``llm_calls`` table for the same audit visibility the
   chat worker enjoys.
2. **Deterministic fallback**: a pure-Python template that mirrors the
   same brief-derived theming logic. Used when (a) no ``code_chat`` is
   wired in (tests, MockWorker runs), (b) the LLM call raises any
   transport/rate-limit error, or (c) the LLM returned HTML that failed
   the sanity validator.

The ASCII output and the ``regions`` list are unchanged across both paths
— the ``mockup_renders`` checkpoint and the screen-reader path depend on
byte-identical ASCII. Same input always produces identical ASCII bytes.

Layer rule: stdlib + harness.models + harness.services.store +
(type-only) harness.services.llm. The ``code_chat`` callable is
type-erased (``Callable``, not ``OpenRouterClient`` instance) so this
module never depends on the LLM client class.
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

# Defaults for the deterministic + LLM-prompt paths. The two-tier defaults
# (neutral vs prompt-default) exist because the prompt asks the LLM to use
# the colors prominently — handing it a richer slate primary reads better
# than passing the dashed-border neutral.
_NEUTRAL_PRIMARY = "#444"
_NEUTRAL_SECONDARY = "#eee"
_PROMPT_DEFAULT_PRIMARY = "#1f2937"  # slate-800
_PROMPT_DEFAULT_SECONDARY = "#f3f4f6"  # gray-100

# How much of the LLM response to keep in the audit log.
_LLM_RESPONSE_LOG_CAP = 50_000

# Sanity validator constants for the LLM-returned HTML.
_HTML_DOCTYPE_PREFIXES = ("<!doctype", "<html")
_HTML_SCRIPT_PATTERN = re.compile(r"<script", re.IGNORECASE)


# Color name → hex map shared between the LLM prompt builder and the
# deterministic fallback so a brief with ``palette={"primary": "blue"}``
# yields identical theming on both paths.
_COLOR_NAMES: dict[str, str] = {
    "blue": "#1e40af",
    "red": "#b91c1c",
    "green": "#16a34a",
    "amber": "#d97706",
    "orange": "#ea580c",
    "yellow": "#ca8a04",
    "purple": "#7e22ce",
    "pink": "#db2777",
    "teal": "#0f766e",
    "cyan": "#0891b2",
    "white": "#ffffff",
    "black": "#111827",
    "gray": "#6b7280",
    "grey": "#6b7280",
    "brown": "#92400e",
    "cream": "#fef3c7",
    "navy": "#1e3a8a",
    "maroon": "#7f1d1d",
    "olive": "#65a30d",
    "lime": "#84cc16",
}


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
# Color normalization
# ---------------------------------------------------------------------------


def _normalize_color(value: object, default: str) -> str:
    """Accept hex (``#RRGGBB`` / ``#RGB``), a color name, or ``None``.

    Returns a valid hex string; falls back to ``default`` for invalid
    input. Used in BOTH the LLM prompt (so the model gets clean hex
    regardless of how the brief expressed the color) and the deterministic
    fallback (so the rendered HTML's inline styles stay safe).

    Hex inputs are returned with their original case preserved (matters
    because downstream tests assert on the brief's exact hex spelling and
    operators may have preferred capitalization). Only name lookups use
    the lowercased form.
    """
    if not isinstance(value, str):
        return default
    trimmed = value.strip()
    if not trimmed:
        return default
    lowered = trimmed.lower()
    if lowered in _COLOR_NAMES:
        return _COLOR_NAMES[lowered]
    if _HEX_COLOR_RE.match(trimmed):
        return trimmed
    return default


def _safe_hex(value: object, fallback: str) -> str:
    """Strict hex check — used by the deterministic renderer's inline styles.

    Distinct from ``_normalize_color`` because the deterministic path used
    to (and still does, for backwards compatibility) reject anything that
    isn't already valid hex. The new ``_normalize_color`` is the looser
    entry point for the brief-color resolution flow that BOTH paths share.
    """
    if isinstance(value, str) and _HEX_COLOR_RE.match(value):
        return value
    return fallback


# ---------------------------------------------------------------------------
# HTML wireframe generation — deterministic fallback
# ---------------------------------------------------------------------------


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


def _resolve_palette(brief: dict | None) -> tuple[str, str]:
    """Return (primary_hex, secondary_hex) resolved from the brief palette.

    Color names are accepted via ``_normalize_color`` so the LLM prompt and
    the deterministic renderer agree on how a brief like
    ``palette={"primary": "blue", "secondary": "white"}`` themes.
    """
    palette: dict[str, object] = {}
    if isinstance(brief, dict):
        raw_palette = brief.get("palette")
        if isinstance(raw_palette, dict):
            palette = raw_palette
    primary = _normalize_color(palette.get("primary"), _NEUTRAL_PRIMARY)
    secondary = _normalize_color(palette.get("secondary"), _NEUTRAL_SECONDARY)
    return primary, secondary


def _build_mockup_html_deterministic(
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

    primary_resolved, secondary_resolved = _resolve_palette(brief)
    # Reapply the strict hex guard to the resolved values so inline-style
    # insertion stays inside the closed set even if a future change broadens
    # `_normalize_color`.
    primary = _safe_hex(primary_resolved, _NEUTRAL_PRIMARY)
    secondary = _safe_hex(secondary_resolved, _NEUTRAL_SECONDARY)

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


# ---------------------------------------------------------------------------
# LLM path — prompt construction + sanity validator + dispatcher
# ---------------------------------------------------------------------------


_LLM_SYSTEM_MESSAGE = (
    "You generate clean, self-contained HTML wireframe mockups. Reply with"
    " ONLY the HTML document, no other text."
)


def _section_names(sections: list[dict]) -> list[str]:
    """Project the cleaned section list to its display-name list."""
    return [s["name"] for s in sections if isinstance(s.get("name"), str)]


def _build_llm_prompt(
    sections: list[dict],
    primary_cta: str | None,
    brief: dict | None,
) -> list[dict]:
    """Build the chat messages for the code-LLM mockup request.

    Resolves color names → hex BEFORE templating so the model never has to
    guess what 'blue' means. Brief-derived strings are inserted verbatim
    (the LLM sees them as content, not as HTML to escape — the output
    document is validated for safety afterwards).
    """
    primary_hex, secondary_hex = _resolve_palette(brief)
    if primary_hex == _NEUTRAL_PRIMARY:
        primary_hex = _PROMPT_DEFAULT_PRIMARY
    if secondary_hex == _NEUTRAL_SECONDARY:
        secondary_hex = _PROMPT_DEFAULT_SECONDARY

    name = _brief_field(brief, "name") or "(unnamed business)"
    tagline = _brief_field(brief, "tagline") or ""
    industry = _brief_field(brief, "industry") or ""
    audience = _brief_field(brief, "audience") or ""
    aesthetic = _brief_field(brief, "aesthetic") or "clean/professional"

    cta = primary_cta if isinstance(primary_cta, str) and primary_cta else None
    if cta is None:
        cta = _brief_field(brief, "primary_cta") or "Contact us"

    section_names = _section_names(sections)

    # Include all remaining brief fields so the LLM uses real business
    # details (phone, address, hours, services, etc.) instead of inventing.
    _BRIEF_CORE_KEYS = {
        "name", "tagline", "industry", "audience", "aesthetic",
        "primary_cta", "palette",
    }
    extra_lines: list[str] = []
    if isinstance(brief, dict):
        for k, v in brief.items():
            if k in _BRIEF_CORE_KEYS or not v:
                continue
            label = k.replace("_", " ").title()
            if isinstance(v, list):
                extra_lines.append(f"- {label}: {', '.join(str(x) for x in v)}")
            else:
                extra_lines.append(f"- {label}: {v}")
    extra_block = "\n".join(extra_lines)

    prompt = (
        "You are generating a wireframe HTML mockup for a small-business"
        " website.\n\n"
        "Business:\n"
        f"- Name: {name}\n"
        f"- Tagline: {tagline}\n"
        f"- Industry: {industry}\n"
        f"- Audience: {audience}\n"
        f"- Primary CTA: {cta}\n"
        f"- Sections: {section_names}\n"
        + (f"{extra_block}\n" if extra_block else "")
        + "\n"
        "Visual style:\n"
        f"- Color palette: primary={primary_hex}, secondary={secondary_hex}\n"
        f"- Aesthetic: {aesthetic}\n\n"
        "Layout requirements:\n"
        "- DESKTOP layout, target width 900px (the preview renders in a"
        " 900px iframe)\n"
        "- Full-width header band using the primary color\n"
        "- Hero section below header: business name as a large heading,"
        " tagline, and a primary CTA button\n"
        "- Body sections (everything between hero and footer) arranged in"
        " a desktop-appropriate way — use a 2- or 3-column grid for"
        " service/reviews/feature sections, full-width for media sections,"
        " etc. NOT a vertical mobile stack\n"
        "- Footer at the bottom in a muted color\n"
        "- Generous whitespace, large readable type\n\n"
        "Hard constraints:\n"
        "- ONE complete HTML document starting with <!doctype html>\n"
        "- Inline <style> only; NO external CSS, NO external fonts, NO @import\n"
        "- ZERO JavaScript anywhere\n"
        "- ZERO external resources (no images, no remote URLs, no <link>"
        " tags except favicon optional)\n"
        '- Sandbox-safe (the document will be embedded in <iframe sandbox="">)\n'
        "- Use the palette colors prominently\n"
        "- Use the EXACT business details provided above (name, tagline,"
        " phone, address, hours, etc.) — NEVER invent or substitute"
        " placeholder content\n\n"
        "Respond with ONLY the HTML document. No markdown fences, no"
        " preamble, no explanation. Just the HTML."
    )

    return [
        {"role": "system", "content": _LLM_SYSTEM_MESSAGE},
        {"role": "user", "content": prompt},
    ]


def _validate_llm_html(text: object) -> bool:
    """Return True iff ``text`` looks like a safe self-contained document.

    Two cheap checks: must start with ``<!doctype`` or ``<html`` (after
    whitespace, case-insensitive); must NOT contain ``<script``
    (case-insensitive). The script check is a hard XSS guard — we drop
    anything that smells of JS, even if the iframe sandbox would block
    execution anyway, because the document body is HTML-escaped into the
    chat panel's srcdoc attribute and we don't want bytes that LOOK
    executable surviving that round-trip.
    """
    if not isinstance(text, str):
        return False
    stripped = text.lstrip().lower()
    if not stripped.startswith(_HTML_DOCTYPE_PREFIXES):
        return False
    if _HTML_SCRIPT_PATTERN.search(text):
        return False
    return True


def _truncate_for_log(text: str) -> str:
    if len(text) <= _LLM_RESPONSE_LOG_CAP:
        return text
    return text[:_LLM_RESPONSE_LOG_CAP]


def _record_llm_call(
    ctx: "ToolContext",
    *,
    messages: list[dict],
    response_text: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    status: str,
    error_message: str | None,
) -> None:
    """Persist one mockup-LLM attempt to the per-session ``llm_calls`` table.

    Hard-codes ``related_event_id`` to the latest ``worker_input`` event id
    as an approximation — render_mockup IS the tool_call that follows a
    worker_input, so linking back to the input event groups the mockup LLM
    call under the same UI turn as the chat worker call that emitted it.
    Best-effort: a logging failure must not break the tool, so any
    exception here is swallowed.
    """
    try:
        store.record_llm_call(
            ctx.session_conn,
            model=ctx.code_model or "(unknown-code-model)",
            is_fallback=ctx.code_model_is_fallback,
            request_messages=messages,
            request_options={"temperature": 0.4, "purpose": "render_mockup"},
            response_text=_truncate_for_log(response_text)
            if response_text is not None
            else None,
            finish_reason=None,
            tokens_in=int(tokens_in),
            tokens_out=int(tokens_out),
            cost_usd=float(cost_usd),
            status=status,
            error_message=error_message,
            related_event_id=store.latest_worker_input_event_id(
                ctx.session_conn
            ),
        )
    except Exception:
        # Logging is a side-channel; never let it sink the tool.
        pass


def _try_llm_html(
    ctx: "ToolContext",
    sections: list[dict],
    primary_cta: str | None,
    brief: dict | None,
) -> str | None:
    """Attempt the LLM path. Return validated HTML on success, ``None`` else.

    All exceptions are caught — the deterministic renderer is the safety
    net, and the audit log records exactly which failure mode hit (rate
    limit / transport / parse / unknown).
    """
    if ctx.code_chat is None:
        return None

    messages = _build_llm_prompt(sections, primary_cta, brief)

    # Lazy import keeps the layer rule clean: tools/mockup.py only sees the
    # LLM exception types under TYPE_CHECKING for static analysis. We
    # import them inside the try block at call time for the runtime path.
    try:
        from harness.services.llm import (
            LLMTransportError,
            RateLimited,
        )
    except Exception:
        # Defensive: if the LLM module fails to import for any reason,
        # fall back deterministically rather than crashing the tool.
        return None

    try:
        response = ctx.code_chat(
            messages=messages,
            response_format=None,
            temperature=0.4,
        )
    except RateLimited as exc:
        _record_llm_call(
            ctx,
            messages=messages,
            response_text=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status="rate_limited",
            error_message=str(exc),
        )
        return None
    except LLMTransportError as exc:
        _record_llm_call(
            ctx,
            messages=messages,
            response_text=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status="transport_error",
            error_message=str(exc),
        )
        return None
    except Exception as exc:
        _record_llm_call(
            ctx,
            messages=messages,
            response_text=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status="transport_error",
            error_message=f"unexpected {type(exc).__name__}: {exc}",
        )
        return None

    text = getattr(response, "text", None)
    tokens_in = int(getattr(response, "tokens_in", 0) or 0)
    tokens_out = int(getattr(response, "tokens_out", 0) or 0)
    cost_usd = float(getattr(response, "cost_usd", 0.0) or 0.0)

    if _validate_llm_html(text):
        # Type narrowing: _validate_llm_html guarantees text is a str here.
        assert isinstance(text, str)
        _record_llm_call(
            ctx,
            messages=messages,
            response_text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            status="ok",
            error_message=None,
        )
        return text

    # Call succeeded transport-wise but the body failed the sanity check.
    # Log as parse_error per the spec — the call IS recorded, its output
    # was just rejected.
    _record_llm_call(
        ctx,
        messages=messages,
        response_text=text if isinstance(text, str) else None,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        status="parse_error",
        error_message=(
            "mockup HTML failed sanity validation"
            " (missing doctype/html prefix or contains <script>)"
        ),
    )
    return None


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
    brief_present = brief is not None and bool(brief)

    # Try the LLM path first; if it returns None, fall back deterministically.
    llm_html = _try_llm_html(ctx, cleaned, primary_cta, brief)
    if llm_html is not None:
        html_doc = llm_html
        # The LLM was prompted with the brief; if we had one, the document
        # IS themed by definition. If no brief was on file the prompt still
        # ran (with placeholder fields) — call that "themed" only when the
        # brief actually existed, matching the deterministic-path semantics.
        themed = brief_present
    else:
        html_doc = _build_mockup_html_deterministic(cleaned, primary_cta, brief)
        themed = brief_present

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
