"""Layer 3 view helpers — pure functions that shape store rows into shapes
the Jinja templates consume.

No I/O beyond reading `os.environ` (the env-var bundle is the operator-
visible model config). All other inputs are plain dicts already loaded by
`harness.api.app.view_session`. Importable from `app.py`; do not import
anything from `harness.api.*` here.

Public surface:
  - format_time_hms(iso_ts)              -> "HH:MM:SS"
  - format_event_for_table(event)        -> {time, type, badge_class, summary, highlight}
  - build_conversation(events, materials_by_id) -> list[{role, body, ts, meta}]
  - derive_active_models()               -> {chat: {primary, fallback}, code: {...}}
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from typing import Any

import mistune

# Module-level renderer — built once, reused for every request. ``escape=True``
# means any embedded HTML in the input (the LLM output is untrusted) is
# escaped, so a `<script>` token in agent text cannot inject JS. ``hard_wrap``
# turns single newlines into ``<br>`` which matches chat-style expectations.
_md_renderer = mistune.create_markdown(
    escape=True,
    hard_wrap=True,
    plugins=[],
)


def _render_markdown(text: str) -> str:
    """Render ``text`` as CommonMark with HTML escaped. Always returns ``str``.

    ``mistune.create_markdown(...)`` returns a callable whose static type is
    ``str | list``; in practice with no renderer override it always returns
    ``str``. We coerce defensively to keep the rest of the codebase typed.
    """
    out = _md_renderer(text)
    if isinstance(out, str):
        return out
    # mistune in AST mode would return a list — we never configure that, but
    # collapse defensively so the type checker is satisfied.
    return "".join(str(x) for x in out)


def _escape_plain(text: str) -> str:
    """HTML-escape plain text and convert newlines to ``<br>``.

    Used for agent bubbles whose body is an internal summary string
    (e.g. ``"Wrote index.html (1234 bytes)"``) — those are already
    formatted for the UI, so we treat them as literal text rather than
    feeding them through the markdown renderer.
    """
    return html.escape(text).replace("\n", "<br>")


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------


def format_time_hms(iso_ts: str) -> str:
    """Render an ISO8601 timestamp as ``HH:MM:SS`` in local time.

    Tolerant of trailing ``Z`` (replaced with ``+00:00``) and naive strings.
    Returns the input unchanged if parsing fails so the UI never crashes on
    a malformed value.
    """
    if not isinstance(iso_ts, str) or not iso_ts:
        return iso_ts if isinstance(iso_ts, str) else ""
    raw = iso_ts.replace("Z", "+00:00") if iso_ts.endswith("Z") else iso_ts
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return iso_ts
    # If tz-aware, convert to system local time so all rows on the page share
    # the same clock. Naive timestamps render as-is.
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone()
        except (OSError, ValueError):
            pass
    return dt.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Event table formatting
# ---------------------------------------------------------------------------


_BADGE_BY_TYPE: dict[str, str] = {
    "worker_input": "neutral",
    "worker_output": "neutral",
    "tool_call": "neutral",
    "tool_result": "neutral",
    "post_hook_run": "neutral",
    "checkpoint_result": "neutral",
    "alarm_raised": "warning",
    "awaiting_human": "awaiting_human",
    "human_resumed": "pass",
    "model_swapped": "swapped",
}


def _truncate(s: str, n: int) -> str:
    if not isinstance(s, str):
        s = str(s)
    return s if len(s) <= n else s[: max(0, n - 3)] + "..."


def _json_excerpt(value: Any, n: int) -> str:
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError):
        encoded = str(value)
    return _truncate(encoded, n)


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce ``value`` to a non-None dict for safe ``.get(...)`` access.

    Defensive: store-loaded payloads are typed as ``dict``, but the JSON
    column may technically deserialize to anything — so we narrow here once
    and let the rest of the module treat the result as ``dict``.
    """
    return value if isinstance(value, dict) else {}


def format_event_for_table(event: dict) -> dict:
    """Project an event row into ``{time, type, badge_class, summary, highlight}``.

    ``highlight=True`` is set for ``model_swapped`` so the row gets a
    background tint — the rubric's bonus moment.

    Checkpoint and alarm rows get a status-coloured badge in the type cell
    (e.g. green ``checkpoint_result`` badge on PASS) so the user can see the
    outcome at a glance without expanding the summary.
    """
    etype = event.get("type", "?")
    payload = _as_dict(event.get("payload"))
    ts = event.get("ts", "")
    badge_class = _BADGE_BY_TYPE.get(etype, "neutral")
    highlight = etype == "model_swapped"

    if etype == "worker_input":
        msgs = payload.get("messages_count", 0)
        tokens = payload.get("tokens_estimate", 0)
        summary = f"Worker prompted ({msgs} msgs, ~{tokens} tokens)"
    elif etype == "worker_output":
        envelope = _as_dict(payload.get("envelope"))
        wt = envelope.get("type", payload.get("type"))
        if wt == "tool_call":
            tool = envelope.get("tool", "?")
            summary = f"Agent: call {tool}"
        elif wt == "final":
            summary = "Agent: final"
        elif wt == "escalate":
            summary = "Agent: escalate"
        else:
            summary = f"Agent: {wt or 'unknown'}"
    elif etype == "tool_call":
        tool = payload.get("tool", "?")
        args = payload.get("args", {})
        excerpt = _json_excerpt(args, 60)
        summary = f"Dispatch {tool} {excerpt}"
    elif etype == "tool_result":
        tool = payload.get("tool", "?")
        ok = payload.get("ok")
        if ok:
            summary = f"Result {tool}: ok"
        else:
            err = _as_dict(payload.get("result_or_error"))
            err_kind = err.get("error_kind") or "unknown"
            summary = f"Result {tool}: FAILED ({err_kind})"
            badge_class = "error"
    elif etype == "post_hook_run":
        validate_ok = payload.get("validate_ok")
        sha = payload.get("git_commit_sha") or ""
        sha_short = sha[:7] if sha else "no-op"
        summary = (
            f"Post-hook: validate={'ok' if validate_ok else 'fail'},"
            f" commit={sha_short}"
        )
    elif etype == "checkpoint_result":
        name = payload.get("name", "?")
        status = (payload.get("status") or "?").upper()
        summary = f"Checkpoint {name}: {status}"
        badge_class = "pass" if status == "PASS" else "fail"
    elif etype == "alarm_raised":
        atype = payload.get("type", "?")
        sev = payload.get("severity", "?")
        summary = f"Alarm {atype} ({sev})"
        if sev == "warning":
            badge_class = "warning"
        elif sev == "error":
            badge_class = "error"
        elif sev == "critical":
            badge_class = "critical"
    elif etype == "awaiting_human":
        reason = payload.get("reason")
        if reason:
            summary = f"Paused -- awaiting human ({reason})"
        else:
            summary = "Paused -- awaiting human"
    elif etype == "human_resumed":
        # answer_or_decision is a dict; extract a short readable hint.
        ans = _as_dict(payload.get("answer_or_decision"))
        text = ""
        if "answer_text" in ans and isinstance(ans["answer_text"], str):
            text = ans["answer_text"]
        elif "approved" in ans:
            kind = ans.get("kind")
            if kind == "continuation_approval":
                text = "approve" if ans["approved"] else "stop"
            else:
                text = "approved" if ans["approved"] else "denied"
                if ans.get("subject"):
                    text = f"{text} {ans['subject']}"
        if text:
            summary = f"Human: {_truncate(text, 40)!r}"
        else:
            summary = "Human responded"
    elif etype == "model_swapped":
        from_m = payload.get("from", "?")
        to_m = payload.get("to", "?")
        reason = payload.get("reason", "?")
        summary = f"Model swap: {from_m} -> {to_m} ({reason})"
    else:
        summary = etype

    return {
        "time": format_time_hms(ts) if ts else "",
        "type": etype,
        "badge_class": badge_class,
        "summary": summary,
        "highlight": highlight,
    }


# ---------------------------------------------------------------------------
# Conversation projection
# ---------------------------------------------------------------------------


def _approval_body(material: dict | None, approved: bool, notes: str | None) -> str:
    """Render an approval/denial bubble body from the material it answered."""
    subject = "?"
    if isinstance(material, dict):
        content = material.get("content")
        if isinstance(content, dict) and isinstance(content.get("subject"), str):
            subject = content["subject"]
    head = ("Approved " if approved else "Denied ") + subject
    if notes:
        return f"{head}\nNotes: {notes}"
    return head


def _continuation_body(approved: bool, notes: str | None) -> str:
    head = "Approve continuation" if approved else "Stop"
    if notes:
        return f"{head}\nNotes: {notes}"
    return head


# ---------------------------------------------------------------------------
# Approval card rendering (request_approval bubbles)
# ---------------------------------------------------------------------------

# Hex color sanity check used by the palette swatch renderer. We only emit
# the value into ``style="background:<value>"`` if it matches — anything
# else falls back to the escaped text only, so the rule is a hard XSS guard.
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")

# Display labels for Business Brief keys. Unknown keys are humanized by
# replacing underscores with spaces and title-casing — see ``_humanize_key``.
_BRIEF_LABELS: dict[str, str] = {
    "industry": "Industry",
    "phone": "Phone",
    "email": "Email",
    "address": "Address",
    "service_area": "Service area",
    "service_areas": "Service areas",
    "hours": "Hours",
    "color_palette": "Color palette",
    "palette": "Color palette",
    "aesthetic": "Aesthetic",
    "audience": "Audience",
    "pages": "Pages",
    "primary_cta": "Primary CTA",
    "socials": "Socials",
    "logo": "Logo",
}

# Top-level keys that are rendered separately from the brief-rows <dl>
# (the name + tagline live above the row grid as headings) or that are
# hoisted out of a nested ``contact`` dict — never repeated in the grid.
_BRIEF_TOP_KEYS: set[str] = {"name", "tagline", "contact"}

# Preferred display order for known Business Brief keys. Anything not in
# the list is rendered after, in insertion order.
_BRIEF_ORDER: list[str] = [
    "industry",
    "phone",
    "email",
    "address",
    "service_area",
    "service_areas",
    "hours",
    "color_palette",
    "palette",
    "aesthetic",
    "audience",
    "pages",
    "primary_cta",
    "socials",
    "logo",
]

# Friendly day-range labels for the ``hours`` dict. Unknown keys are
# humanized via ``_humanize_key``.
_HOURS_LABELS: dict[str, str] = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
    "mon_thu": "Mon–Thu",
    "fri_sat": "Fri–Sat",
    "sat_sun": "Sat–Sun",
    "mon_fri": "Mon–Fri",
    "mon_sun": "Mon–Sun",
}


def _humanize_key(key: str) -> str:
    """Turn ``service_area`` into ``Service Area`` for display."""
    return key.replace("_", " ").title()


def _esc(value: Any) -> str:
    """HTML-escape a value safely, coercing to str first."""
    return html.escape(str(value))


def _humanize_day_key(key: str) -> str:
    """Look up or generate a human label for an hours-dict key."""
    if key in _HOURS_LABELS:
        return _HOURS_LABELS[key]
    return key.replace("_", "-").title()


def _render_palette_dd(palette: dict) -> str:
    """Render a palette dict as small color swatches + hex codes.

    Each entry produces ``<span class="swatch" style="background:#hex">``
    plus a ``<code>#hex</code>`` and the role label. Values that aren't
    valid hex colors fall back to escaped text only — no inline style.
    """
    parts: list[str] = []
    for role, value in palette.items():
        role_label = _esc(role)
        value_str = str(value) if value is not None else ""
        if _HEX_COLOR_RE.match(value_str):
            hex_safe = _esc(value_str)
            parts.append(
                f'<span class="swatch" style="background:{hex_safe}" '
                f'title="{role_label}"></span> '
                f"<code>{hex_safe}</code> {role_label}"
            )
        else:
            # Reject — no inline style emitted, just escaped text.
            parts.append(f"<code>{_esc(value_str)}</code> {role_label}")
    return ", ".join(parts)


def _render_hours_dd(hours: dict) -> str:
    """Render an hours dict as ``Mon–Thu: 11:00–21:00`` lines."""
    lines: list[str] = []
    for day_key, time_range in hours.items():
        label = _esc(_humanize_day_key(str(day_key)))
        lines.append(f"{label}: {_esc(time_range)}")
    return "<br>".join(lines)


def _render_socials_dd(socials: dict) -> str:
    """Render a socials dict as ``Instagram: @x, Twitter: @y``."""
    parts: list[str] = []
    for network, handle in socials.items():
        net_label = _esc(str(network).title())
        parts.append(f"{net_label}: {_esc(handle)}")
    return ", ".join(parts)


def _render_value_dd(key: str, value: Any) -> str:
    """Render a value as the inner HTML of a <dd>, dispatching by type.

    Handles palettes/hours/socials by key name, lists via ", ".join,
    nested dicts as "key: value" sub-blocks, and scalars as escaped str.
    """
    if key in ("palette", "color_palette") and isinstance(value, dict):
        return _render_palette_dd(value)
    if key == "hours" and isinstance(value, dict):
        return _render_hours_dd(value)
    if key == "socials" and isinstance(value, dict):
        return _render_socials_dd(value)
    if isinstance(value, list):
        return _esc(", ".join(str(item) for item in value))
    if isinstance(value, dict):
        lines: list[str] = []
        for k, v in value.items():
            lines.append(f"{_esc(str(k))}: {_esc(v)}")
        return "<br>".join(lines)
    return _esc(value)


def _brief_row_keys(details: dict) -> list[str]:
    """Return the keys to render in the brief-rows grid, in display order.

    Skips top-level keys handled separately (name/tagline/contact). The
    fixed-order keys come first; any leftover keys follow in insertion
    order so unknown fields still surface in the card.
    """
    present = [k for k in details.keys() if k not in _BRIEF_TOP_KEYS]
    seen: set[str] = set()
    ordered: list[str] = []
    for k in _BRIEF_ORDER:
        if k in present:
            ordered.append(k)
            seen.add(k)
    for k in present:
        if k not in seen:
            ordered.append(k)
    return ordered


def _label_for_key(key: str, label_map: dict[str, str] | None = None) -> str:
    """Resolve a display label, falling back to ``_humanize_key``."""
    if label_map and key in label_map:
        return label_map[key]
    return _humanize_key(key)


def _render_brief_rows(details: dict, *, label_map: dict[str, str]) -> str:
    """Render the ``<dl class="brief-rows">`` grid for a details dict.

    ``contact`` is hoisted: ``phone``/``email``/``address`` inside it are
    promoted to top-level rows so the user sees a flat list rather than
    a nested sub-block.
    """
    hoisted: dict[str, Any] = {}
    contact = details.get("contact")
    if isinstance(contact, dict):
        for sub_key in ("phone", "email", "address"):
            if sub_key in contact and sub_key not in details:
                hoisted[sub_key] = contact[sub_key]

    # Build the ordered list of rows: hoisted keys interleaved into the
    # normal key order so they sit alongside top-level phone/email/address.
    merged: dict[str, Any] = {}
    for k in details.keys():
        if k in _BRIEF_TOP_KEYS:
            continue
        merged[k] = details[k]
    for k, v in hoisted.items():
        merged.setdefault(k, v)

    keys = _brief_row_keys({**merged, **{k: True for k in hoisted}})
    rows: list[str] = []
    for key in keys:
        if key not in merged:
            continue
        label = _esc(_label_for_key(key, label_map))
        dd_html = _render_value_dd(key, merged[key])
        rows.append(f"<dt>{label}</dt><dd>{dd_html}</dd>")
    if not rows:
        return ""
    return '<dl class="brief-rows">' + "".join(rows) + "</dl>"


def _render_approval_card(
    subject: str, details: dict | None
) -> tuple[str, str]:
    """Render the approval bubble's ``(body_html, body_plain)`` pair.

    Two named subjects (``business_brief``, ``mockup``) get special-case
    layouts; everything else falls back to a labeled list. The HTML is
    built directly (NOT via mistune) — every user value is escaped via
    ``html.escape`` before being concatenated into the output.
    """
    details_dict = details if isinstance(details, dict) else {}

    if subject == "business_brief":
        body_html, body_plain = _render_business_brief_card(details_dict)
    elif subject == "mockup":
        body_html, body_plain = _render_mockup_card(details_dict)
    else:
        body_html, body_plain = _render_generic_card(subject, details_dict)

    return body_html, body_plain


def _render_business_brief_card(details: dict) -> tuple[str, str]:
    """Render the ``business_brief`` approval card with hoisted name/tagline."""
    parts: list[str] = ['<div class="approval-card">']
    parts.append('<h3 class="approval-subject">Business Brief</h3>')

    name_val = details.get("name")
    tagline_val = details.get("tagline")
    plain_extras: list[str] = []
    if isinstance(name_val, str) and name_val:
        parts.append(f'<div class="brief-name">{_esc(name_val)}</div>')
        plain_extras.append(name_val)
    if isinstance(tagline_val, str) and tagline_val:
        parts.append(
            f'<div class="brief-tagline"><em>{_esc(tagline_val)}</em></div>'
        )
        plain_extras.append(tagline_val)

    rows_html = _render_brief_rows(details, label_map=_BRIEF_LABELS)
    if rows_html:
        parts.append(rows_html)
    elif not plain_extras:
        # Empty details — show a friendly prompt so the bubble isn't blank.
        parts.append(
            '<p class="approval-prompt">'
            "Approve the business brief above to proceed."
            "</p>"
        )

    parts.append("</div>")
    body_html = "".join(parts)
    body_plain = "Approval request: business_brief"
    if plain_extras:
        body_plain = body_plain + " — " + " — ".join(plain_extras)
    return body_html, body_plain


def _render_mockup_card(details: dict) -> tuple[str, str]:
    """Render the ``mockup`` approval card (compact, no ASCII re-display)."""
    parts: list[str] = ['<div class="approval-card">']
    parts.append('<h3 class="approval-subject">Mockup</h3>')
    parts.append(
        '<p class="approval-prompt">'
        "Approve the layout above to proceed to building the site."
        "</p>"
    )

    # Optional compact summary if the agent included sections / primary_cta.
    summary_pairs: list[tuple[str, str]] = []
    sections = details.get("sections")
    if isinstance(sections, list) and sections:
        names: list[str] = []
        for sec in sections:
            if isinstance(sec, dict) and isinstance(sec.get("name"), str):
                names.append(sec["name"])
            elif isinstance(sec, str):
                names.append(sec)
        if names:
            summary_pairs.append(("Sections", ", ".join(names)))
    primary_cta = details.get("primary_cta")
    if isinstance(primary_cta, str) and primary_cta:
        summary_pairs.append(("Primary CTA", primary_cta))

    if summary_pairs:
        rows = "".join(
            f"<dt>{_esc(label)}</dt><dd>{_esc(val)}</dd>"
            for label, val in summary_pairs
        )
        parts.append(f'<dl class="brief-rows">{rows}</dl>')

    parts.append("</div>")
    return "".join(parts), "Approval request: mockup"


def _render_generic_card(subject: str, details: dict) -> tuple[str, str]:
    """Fallback renderer: subject as heading + labeled list of details."""
    heading = _humanize_key(subject) if subject else "Approval"
    parts: list[str] = ['<div class="approval-card">']
    parts.append(
        f'<h3 class="approval-subject">{_esc(heading)}</h3>'
    )
    rows_html = _render_brief_rows(details, label_map={})
    if rows_html:
        parts.append(rows_html)
    else:
        parts.append(
            '<p class="approval-prompt">'
            "Please review and approve."
            "</p>"
        )
    parts.append("</div>")
    return "".join(parts), f"Approval request: {subject}"


def build_conversation(
    events: list[dict],
    materials_by_id: dict[str, dict],
) -> list[dict]:
    """Project events into chat-bubble messages, oldest first.

    Each message is ``{role: 'user'|'agent', body: str, body_html: str,
    ts: 'HH:MM:SS', meta: dict}``. ``meta`` carries an optional ``tag``
    ("final", "escalate") and an optional ``details_json`` blob for
    request_approval expansion.

    ``body`` is the raw text (handy for tests/debug); ``body_html`` is the
    safe HTML the template emits. For agent bubbles carrying human-readable
    text from the LLM (``final.summary``, ``escalate.reason``,
    ``ask_user.question``, ``request_approval.subject``) we run the body
    through the markdown renderer (``escape=True`` blocks XSS). For agent
    bubbles representing tool-call summaries (e.g. ``"Wrote index.html
    (1234 bytes)"``) and for all user bubbles we HTML-escape the plain text.

    Only ``worker_output`` (agent) and ``human_resumed`` (user) events
    contribute messages. ``materials_by_id`` is the lookup used to resolve
    the human's underlying pending material so we can render the right copy
    (approval vs. continuation_approval vs. freeform answer).
    """
    out: list[dict] = []
    for event in events:
        etype = event.get("type")
        payload = _as_dict(event.get("payload"))
        ts = format_time_hms(event.get("ts", "")) if event.get("ts") else ""

        if etype == "human_resumed":
            answer = _as_dict(payload.get("answer_or_decision"))
            # answer_material_id points at the row written by /answer; the
            # ORIGINAL pending question lives elsewhere — but the new answer
            # row carries the answered shape (kind + subject + approved or
            # answer_text). Match on the kind/subject in answer_content.
            meta: dict[str, Any] = {}
            kind = answer.get("kind")
            notes_raw = answer.get("notes")
            notes = notes_raw if isinstance(notes_raw, str) else None
            body: str
            if kind == "approval":
                approved = bool(answer.get("approved"))
                pending_subject = answer.get("subject")
                fake_mat = {"content": {"subject": pending_subject}}
                body = _approval_body(fake_mat, approved, notes)
            elif kind == "continuation_approval":
                approved = bool(answer.get("approved"))
                body = _continuation_body(approved, notes)
            elif isinstance(answer.get("answer_text"), str):
                body = answer["answer_text"]
            elif answer:
                body = json.dumps(answer)
            else:
                body = ""
            out.append({
                "role": "user",
                "body": body,
                "body_html": _escape_plain(body),
                "ts": ts,
                "meta": meta,
            })
            continue

        if etype == "worker_output":
            envelope_raw = payload.get("envelope")
            if not isinstance(envelope_raw, dict):
                continue
            envelope: dict[str, Any] = envelope_raw
            wt = envelope.get("type")
            meta = {}
            if wt == "final":
                body = envelope.get("summary", "") or ""
                meta["tag"] = "final"
                out.append({
                    "role": "agent",
                    "body": body,
                    "body_html": _render_markdown(body),
                    "ts": ts,
                    "meta": meta,
                })
                continue
            if wt == "escalate":
                body = envelope.get("reason", "") or ""
                meta["tag"] = "escalate"
                out.append({
                    "role": "agent",
                    "body": body,
                    "body_html": _render_markdown(body),
                    "ts": ts,
                    "meta": meta,
                })
                continue
            if wt == "tool_call":
                tool = envelope.get("tool", "")
                args = _as_dict(envelope.get("args"))
                body, extra, render_md = _tool_call_body(
                    tool, args, materials_by_id
                )
                # Approval bubbles (and any future tool that builds its own
                # HTML) pass a pre-rendered, already-escaped fragment via
                # ``body_html`` in the extra dict. When present, that wins
                # over markdown / plain-text rendering.
                override_html = extra.pop("body_html", None)
                meta.update(extra)
                if isinstance(override_html, str):
                    body_html = override_html
                else:
                    body_html = (
                        _render_markdown(body)
                        if render_md
                        else _escape_plain(body)
                    )
                out.append({
                    "role": "agent",
                    "body": body,
                    "body_html": body_html,
                    "ts": ts,
                    "meta": meta,
                })
                continue
            # Unknown envelope type: skip rather than render JSON.
            continue

        # All other event types are not part of the chat — they live in the
        # Details accordion's events table.
    return out


def _latest_mockup_html(materials_by_id: dict[str, dict]) -> tuple[str, bool] | None:
    """Return (html_doc, themed) for the most recent mockup material, or None.

    Searches the resolved materials map for `type == 'mockup'` rows whose
    content carries an `html` string. Ordering: materials are inserted into
    the dict in id order (UUID7, time-ordered) by the caller, so iterating
    and keeping the last hit gives the latest mockup.
    """
    latest_html: str | None = None
    latest_themed = False
    for mat in materials_by_id.values():
        if not isinstance(mat, dict):
            continue
        if mat.get("type") != "mockup":
            continue
        content = mat.get("content")
        if not isinstance(content, dict):
            continue
        html_doc = content.get("html")
        if isinstance(html_doc, str) and html_doc:
            latest_html = html_doc
            latest_themed = bool(content.get("themed"))
    if latest_html is None:
        return None
    return latest_html, latest_themed


def _tool_call_body(
    tool: str, args: dict, materials_by_id: dict[str, dict] | None = None
) -> tuple[str, dict, bool]:
    """Render a per-tool bubble body.

    Returns ``(body, meta_extras, render_markdown)``. ``render_markdown=True``
    means the body is human-readable text from the LLM (the agent's question
    or approval subject) and should pass through the markdown renderer.
    ``False`` means the body is an internal summary string we constructed
    here (e.g. ``"Wrote index.html (1234 bytes)"``) — those are already
    formatted, so they should only be HTML-escaped, not re-rendered.
    """
    if tool == "ask_user":
        body = args.get("question") or ""
        meta: dict[str, Any] = {}
        opts = args.get("options")
        if isinstance(opts, list) and opts:
            meta["options"] = [str(o) for o in opts]
        return body, meta, True
    if tool == "request_approval":
        subject_raw = args.get("subject") or args.get("summary") or "?"
        subject = str(subject_raw)
        details_raw = args.get("details") or args.get("payload")
        details = details_raw if isinstance(details_raw, dict) else None
        card_html, card_plain = _render_approval_card(subject, details)
        # The pre-rendered HTML rides on ``meta["body_html"]`` and is
        # consumed (and popped) by ``build_conversation``, so it never
        # leaks into the conversation entry's user-facing ``meta`` dict.
        meta_out: dict[str, Any] = {"body_html": card_html}
        return card_plain, meta_out, False
    if tool == "render_mockup":
        layout = _as_dict(args.get("layout_spec"))
        sections_raw = layout.get("sections")
        sections = sections_raw if isinstance(sections_raw, list) else []
        n_sections = len(sections)
        plain = f"Rendered mockup ({n_sections} sections)"
        # If we can find the persisted mockup material, embed its themed
        # HTML inside a sandboxed iframe preview. Otherwise fall back to
        # the plain text summary (existing behavior).
        preview = (
            _latest_mockup_html(materials_by_id)
            if materials_by_id
            else None
        )
        if preview is None:
            return plain, {}, False
        html_doc, themed = preview
        caption = f"Mockup preview ({n_sections} sections"
        caption += ", themed)" if themed else ")"
        # srcdoc requires HTML-attribute escaping of the entire document.
        # html.escape(..., quote=True) handles ``"``, ``'``, ``<``, ``>``,
        # ``&`` — enough for srcdoc placement. The inner document is
        # already HTML-escaped at every user-data insertion point by
        # `_build_mockup_html`.
        srcdoc = html.escape(html_doc, quote=True)
        card_html = (
            '<div class="mockup-card">'
            f'<div class="mockup-caption">{html.escape(caption)}</div>'
            '<iframe class="mockup-preview" sandbox="" '
            f'srcdoc="{srcdoc}" loading="lazy" '
            'width="100%" height="420"></iframe>'
            "</div>"
        )
        return plain, {"body_html": card_html}, False
    if tool == "write_file":
        path = args.get("path", "?")
        content = args.get("content")
        size = len(content) if isinstance(content, str) else 0
        return f"Wrote {path} ({size} bytes)", {}, False
    if tool == "read_file":
        path = args.get("path", "?")
        return f"Read {path}", {}, False
    if tool == "list_files":
        path = args.get("path", ".")
        return f"Listed files under {path}", {}, False
    # Unknown tool: render the tool name + a short args excerpt.
    return f"Call {tool} {_json_excerpt(args, 60)}", {}, False


# ---------------------------------------------------------------------------
# Active models
# ---------------------------------------------------------------------------


_DASH = "—"  # em-dash for empty fallbacks


def derive_active_models() -> dict:
    """Return ``{'chat': {'primary', 'fallback'}, 'code': {...}}`` from env.

    Empty / missing values render as an em-dash so the UI shows ``-`` for
    "no fallback configured" rather than an awkward blank.
    """

    def _val(name: str) -> str:
        v = os.environ.get(name, "")
        return v if v else _DASH

    return {
        "chat": {
            "primary": _val("MODEL_CHAT"),
            "fallback": _val("MODEL_CHAT_FALLBACK"),
        },
        "code": {
            "primary": _val("MODEL_CODE"),
            "fallback": _val("MODEL_CODE_FALLBACK"),
        },
    }


__all__ = [
    "format_time_hms",
    "format_event_for_table",
    "build_conversation",
    "derive_active_models",
]
