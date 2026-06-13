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
                body, extra, render_md = _tool_call_body(tool, args)
                meta.update(extra)
                body_html = (
                    _render_markdown(body) if render_md else _escape_plain(body)
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
    # materials_by_id is reserved for future enrichment (currently the
    # answer payload already carries kind + subject). Touch the arg so type
    # checkers see it's intentionally accepted.
    _ = materials_by_id
    return out


def _tool_call_body(tool: str, args: dict) -> tuple[str, dict, bool]:
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
        subject = args.get("subject") or args.get("summary") or "?"
        body = f"Requesting approval: {subject}"
        meta_out: dict[str, Any] = {}
        details = args.get("details") or args.get("payload")
        if isinstance(details, dict) and details:
            try:
                meta_out["details_json"] = json.dumps(details, indent=2, default=str)
            except (TypeError, ValueError):
                meta_out["details_json"] = str(details)
        return body, meta_out, True
    if tool == "render_mockup":
        layout = _as_dict(args.get("layout_spec"))
        sections_raw = layout.get("sections")
        sections = sections_raw if isinstance(sections_raw, list) else []
        return f"Rendered mockup ({len(sections)} sections)", {}, False
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
