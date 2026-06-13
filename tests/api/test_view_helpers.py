"""Unit tests for `harness.api.view_helpers`.

Pure-function tests — no DB, no HTTP, no LLM. Each function under test
operates on plain dicts (or `os.environ` for `derive_active_models`).
"""

from __future__ import annotations

import re

import pytest

from harness.api import view_helpers


# ---------------------------------------------------------------------------
# format_time_hms
# ---------------------------------------------------------------------------


class TestFormatTimeHms:
    def test_iso_with_offset(self) -> None:
        # UTC-anchored input. The function converts tz-aware times to local
        # time before formatting, so we assert only on the HH:MM:SS shape.
        out = view_helpers.format_time_hms("2026-06-13T14:23:45+00:00")
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", out), out

    def test_iso_z_suffix(self) -> None:
        out = view_helpers.format_time_hms("2026-06-13T01:02:03Z")
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", out), out

    def test_iso_with_microseconds(self) -> None:
        out = view_helpers.format_time_hms("2026-06-13T14:23:45.123456+00:00")
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", out), out

    def test_naive_iso(self) -> None:
        # A naive timestamp is treated as local — no tz conversion attempted.
        out = view_helpers.format_time_hms("2026-06-13T07:08:09")
        assert out == "07:08:09"

    def test_defensive_on_garbage(self) -> None:
        # The UI must never crash on malformed input — return unchanged.
        assert view_helpers.format_time_hms("not a date") == "not a date"
        assert view_helpers.format_time_hms("") == ""

    def test_non_string_returns_empty(self) -> None:
        # Pyright will complain about the cast — runtime should still be safe.
        assert view_helpers.format_time_hms(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# format_event_for_table
# ---------------------------------------------------------------------------


def _event(etype: str, payload: dict, ts: str = "2026-06-13T12:00:00+00:00") -> dict:
    return {"id": "x", "ts": ts, "type": etype, "stage": "build", "payload": payload}


class TestFormatEventForTable:
    def test_worker_input(self) -> None:
        ev = _event(
            "worker_input",
            {"model": "m", "messages_count": 7, "tokens_estimate": 1234},
        )
        out = view_helpers.format_event_for_table(ev)
        assert out["summary"] == "Worker prompted (7 msgs, ~1234 tokens)"
        assert out["type"] == "worker_input"
        assert out["highlight"] is False

    def test_worker_output_tool_call(self) -> None:
        ev = _event(
            "worker_output",
            {"envelope": {"type": "tool_call", "tool": "write_file", "args": {}}},
        )
        assert (
            view_helpers.format_event_for_table(ev)["summary"]
            == "Agent: call write_file"
        )

    def test_worker_output_final(self) -> None:
        ev = _event("worker_output", {"envelope": {"type": "final", "summary": "ok"}})
        assert view_helpers.format_event_for_table(ev)["summary"] == "Agent: final"

    def test_worker_output_escalate(self) -> None:
        ev = _event("worker_output", {"envelope": {"type": "escalate", "reason": "x"}})
        assert (
            view_helpers.format_event_for_table(ev)["summary"] == "Agent: escalate"
        )

    def test_tool_call_with_args_excerpt(self) -> None:
        ev = _event("tool_call", {"tool": "write_file", "args": {"path": "a.html"}})
        out = view_helpers.format_event_for_table(ev)
        assert out["summary"].startswith("Dispatch write_file ")
        # Args excerpt is capped — assert reasonable length on a long arg.
        long_args = {"big": "x" * 500}
        out2 = view_helpers.format_event_for_table(
            _event("tool_call", {"tool": "x", "args": long_args})
        )
        # Cap is 60 (per spec); summary = "Dispatch <tool> <excerpt>"
        assert len(out2["summary"]) < 100

    def test_tool_result_ok(self) -> None:
        ev = _event("tool_result", {"tool": "render_mockup", "ok": True})
        out = view_helpers.format_event_for_table(ev)
        assert out["summary"] == "Result render_mockup: ok"

    def test_tool_result_failed_sets_error_badge(self) -> None:
        ev = _event(
            "tool_result",
            {
                "tool": "write_file",
                "ok": False,
                "result_or_error": {"error_kind": "denied", "error_message": "no"},
            },
        )
        out = view_helpers.format_event_for_table(ev)
        assert "FAILED" in out["summary"]
        assert "denied" in out["summary"]
        assert out["badge_class"] == "error"

    def test_post_hook_run_with_sha(self) -> None:
        ev = _event(
            "post_hook_run",
            {"validate_ok": True, "seo_regenerated": True, "git_commit_sha": "abc1234ef"},
        )
        out = view_helpers.format_event_for_table(ev)
        assert "validate=ok" in out["summary"]
        assert "commit=abc1234" in out["summary"]

    def test_post_hook_run_no_op_when_sha_missing(self) -> None:
        ev = _event(
            "post_hook_run",
            {"validate_ok": False, "seo_regenerated": False, "git_commit_sha": None},
        )
        out = view_helpers.format_event_for_table(ev)
        assert "validate=fail" in out["summary"]
        assert "commit=no-op" in out["summary"]

    def test_checkpoint_pass_badge_green(self) -> None:
        ev = _event(
            "checkpoint_result",
            {"name": "site_valid", "status": "pass", "criteria_results": {}},
        )
        out = view_helpers.format_event_for_table(ev)
        assert out["summary"] == "Checkpoint site_valid: PASS"
        assert out["badge_class"] == "pass"

    def test_checkpoint_fail_badge_red(self) -> None:
        ev = _event(
            "checkpoint_result",
            {"name": "site_valid", "status": "fail", "criteria_results": {}},
        )
        out = view_helpers.format_event_for_table(ev)
        assert out["badge_class"] == "fail"

    @pytest.mark.parametrize(
        "severity,expected_class",
        [("warning", "warning"), ("error", "error"), ("critical", "critical")],
    )
    def test_alarm_severity_maps_to_badge(self, severity: str, expected_class: str) -> None:
        ev = _event(
            "alarm_raised",
            {"type": "tool_failed", "severity": severity},
        )
        out = view_helpers.format_event_for_table(ev)
        assert out["badge_class"] == expected_class
        assert severity in out["summary"]

    def test_awaiting_human_with_reason(self) -> None:
        ev = _event("awaiting_human", {"reason": "request_approval"})
        out = view_helpers.format_event_for_table(ev)
        assert "request_approval" in out["summary"]

    def test_human_resumed_freeform_answer_truncated(self) -> None:
        ev = _event(
            "human_resumed",
            {
                "material_id": "m1",
                "answer_or_decision": {
                    "answer_text": "Maria's Pizza Palace, family-owned, downtown",
                },
            },
        )
        out = view_helpers.format_event_for_table(ev)
        assert out["summary"].startswith("Human: ")
        assert "Maria" in out["summary"]
        # Truncated at 40 chars.
        assert len(out["summary"]) <= 60

    def test_human_resumed_approval(self) -> None:
        ev = _event(
            "human_resumed",
            {
                "material_id": "m1",
                "answer_or_decision": {
                    "kind": "approval",
                    "approved": True,
                    "subject": "mockup",
                },
            },
        )
        out = view_helpers.format_event_for_table(ev)
        assert "approved" in out["summary"]
        assert "mockup" in out["summary"]

    def test_model_swapped_highlight(self) -> None:
        ev = _event(
            "model_swapped",
            {"from": "free/m", "to": "paid/m", "reason": "rate_limited"},
        )
        out = view_helpers.format_event_for_table(ev)
        assert out["highlight"] is True
        assert "free/m" in out["summary"]
        assert "paid/m" in out["summary"]

    def test_unknown_event_type_falls_through(self) -> None:
        ev = _event("brand_new_event_type", {})
        out = view_helpers.format_event_for_table(ev)
        assert out["summary"] == "brand_new_event_type"
        assert out["highlight"] is False

    def test_format_event_for_table_handles_rewound(self) -> None:
        ev = _event(
            "rewound",
            {
                "target_event_id": "0190a8d4-9c20-7000-8000-000000000abc",
                "removed_events": 7,
                "removed_materials": 2,
                "removed_checkpoints": 1,
                "removed_alarms": 0,
                "repended_material_id": "mat-x",
            },
        )
        out = view_helpers.format_event_for_table(ev)
        assert "Rewound" in out["summary"]
        assert "0190a8d4" in out["summary"]
        assert "7" in out["summary"]
        assert out["highlight"] is True
        # The event id is exposed so the template can render data-event-id.
        assert "id" in out


# ---------------------------------------------------------------------------
# build_conversation
# ---------------------------------------------------------------------------


class TestBuildConversation:
    def test_three_turn_flow(self) -> None:
        """ask_user -> answer (freeform) -> request_approval -> approve ->
        write_file tool_call -> final.

        Verifies role alternation and per-tool body templates.
        """
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "ask_user",
                        "args": {"question": "What is your business name?"},
                    }
                },
                ts="2026-06-13T12:00:01+00:00",
            ),
            _event(
                "human_resumed",
                {
                    "material_id": "ans1",
                    "answer_or_decision": {"answer_text": "Maria's Pizza"},
                },
                ts="2026-06-13T12:00:02+00:00",
            ),
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "request_approval",
                        "args": {"subject": "mockup", "details": {"k": "v"}},
                    }
                },
                ts="2026-06-13T12:00:03+00:00",
            ),
            _event(
                "human_resumed",
                {
                    "material_id": "ans2",
                    "answer_or_decision": {
                        "kind": "approval",
                        "approved": True,
                        "subject": "mockup",
                    },
                },
                ts="2026-06-13T12:00:04+00:00",
            ),
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "write_file",
                        "args": {"path": "index.html", "content": "<html/>" * 10},
                    }
                },
                ts="2026-06-13T12:00:05+00:00",
            ),
            _event(
                "worker_output",
                {"envelope": {"type": "final", "summary": "Site built."}},
                ts="2026-06-13T12:00:06+00:00",
            ),
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert [m["role"] for m in msgs] == [
            "agent",
            "user",
            "agent",
            "user",
            "agent",
            "agent",
        ]
        # ask_user bubble = the question text.
        assert msgs[0]["body"] == "What is your business name?"
        # Freeform answer = literal text.
        assert msgs[1]["body"] == "Maria's Pizza"
        # request_approval bubble: plain body summarises the request, and
        # body_html carries the rendered approval card (subject-aware) —
        # no JSON expander, no `<details>` wrapper.
        assert msgs[2]["body"].startswith("Approval request: mockup")
        assert "details_json" not in msgs[2]["meta"]
        assert "body_html" not in msgs[2]["meta"]
        assert "approval-card" in msgs[2]["body_html"]
        # Approval user bubble = "Approved <subject>".
        assert msgs[3]["body"].startswith("Approved mockup")
        # write_file bubble = "Wrote {path} ({bytes} bytes)".
        assert msgs[4]["body"].startswith("Wrote index.html (")
        # Final bubble has the final tag.
        assert msgs[5]["body"] == "Site built."
        assert msgs[5]["meta"].get("tag") == "final"

    def test_continuation_approval_renders_approve_or_stop(self) -> None:
        events = [
            _event(
                "human_resumed",
                {
                    "material_id": "ans1",
                    "answer_or_decision": {
                        "kind": "continuation_approval",
                        "approved": True,
                    },
                },
            ),
            _event(
                "human_resumed",
                {
                    "material_id": "ans2",
                    "answer_or_decision": {
                        "kind": "continuation_approval",
                        "approved": False,
                    },
                },
            ),
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert msgs[0]["body"].startswith("Approve continuation")
        assert msgs[1]["body"].startswith("Stop")

    def test_ask_user_with_options_carries_them(self) -> None:
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "ask_user",
                        "args": {
                            "question": "Aesthetic?",
                            "options": ["modern", "rustic"],
                        },
                    }
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert msgs[0]["meta"]["options"] == ["modern", "rustic"]

    def test_render_mockup_counts_sections(self) -> None:
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "render_mockup",
                        "args": {
                            "layout_spec": {
                                "sections": [{"name": "hero"}, {"name": "footer"}]
                            }
                        },
                    }
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert msgs[0]["body"] == "Rendered mockup (2 sections)"

    def test_render_mockup_bubble_includes_iframe_with_srcdoc(self) -> None:
        """When the persisted mockup material is available in materials_by_id,
        the conversation entry carries the sandboxed iframe in
        ``attachment_html`` (not in the bubble body) so it can render
        full-width under the bubble. The bubble itself shows only the
        caption.
        """
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "render_mockup",
                        "args": {
                            "layout_spec": {
                                "sections": [{"name": "Hero"}, {"name": "Footer"}]
                            }
                        },
                    }
                },
            )
        ]
        materials_by_id = {
            "mat-1": {
                "id": "mat-1",
                "type": "mockup",
                "content": {
                    "ascii": "+--+\n|x|\n+--+",
                    "regions": ["Hero", "Footer"],
                    "html": "<!doctype html><html><body><h1>UNIQUE_BRAND_TEST</h1></body></html>",
                    "themed": True,
                },
            }
        }
        msgs = view_helpers.build_conversation(events, materials_by_id)
        # Bubble body holds the caption only — no iframe, no srcdoc.
        body_html = msgs[0]["body_html"]
        assert "<iframe" not in body_html
        assert "srcdoc=" not in body_html
        # The themed-suffix appears in the caption.
        assert "themed" in body_html
        # Iframe + srcdoc are now in the attachment slot.
        attachment_html = msgs[0]["attachment_html"]
        assert "<iframe" in attachment_html
        assert 'sandbox=""' in attachment_html
        assert "srcdoc=" in attachment_html
        # The escaped HTML doc lives in the srcdoc.
        assert "UNIQUE_BRAND_TEST" in attachment_html
        # The doctype angle brackets must be escaped inside the attribute.
        assert "&lt;!doctype" in attachment_html
        # The plain body still summarises the call.
        assert msgs[0]["body"] == "Rendered mockup (2 sections)"

    def test_render_mockup_bubble_iframe_escapes_srcdoc(self) -> None:
        """srcdoc placement requires HTML-attribute escaping. A document
        containing `"` characters must not break the attribute boundary.
        Iframe lives in attachment_html now, not in body_html.
        """
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "render_mockup",
                        "args": {
                            "layout_spec": {"sections": [{"name": "x"}]},
                        },
                    }
                },
            )
        ]
        materials_by_id = {
            "mat-1": {
                "id": "mat-1",
                "type": "mockup",
                "content": {
                    "ascii": "+--+",
                    "regions": ["x"],
                    "html": '<!doctype html><html><body title="quote\\"break">ok</body></html>',
                    "themed": False,
                },
            }
        }
        msgs = view_helpers.build_conversation(events, materials_by_id)
        attachment_html = msgs[0]["attachment_html"]
        # Find the srcdoc attribute opener.
        srcdoc_opener = 'srcdoc="'
        start = attachment_html.find(srcdoc_opener)
        assert start != -1
        # Walk forward from the opener and find the FIRST raw double-quote
        # — that's the attribute's closing delimiter. Everything between
        # must be HTML-attribute-escaped (no raw `"`).
        attr_value_start = start + len(srcdoc_opener)
        closing_quote_at = attachment_html.find('"', attr_value_start)
        assert closing_quote_at != -1
        attr_value = attachment_html[attr_value_start:closing_quote_at]
        # The original document had `"` characters; they must be encoded.
        assert '"' not in attr_value
        assert "&quot;" in attr_value
        # And the angle brackets of the inner doctype are escaped too.
        assert "&lt;" in attr_value

    def test_render_mockup_bubble_falls_back_to_plain_without_material(self) -> None:
        """Without a mockup material in materials_by_id, the bubble falls back
        to the original plain-text summary (no iframe, no body_html override,
        no attachment_html).
        """
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "render_mockup",
                        "args": {
                            "layout_spec": {"sections": [{"name": "h"}, {"name": "f"}]},
                        },
                    }
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert "<iframe" not in body_html
        assert "Rendered mockup (2 sections)" in body_html
        assert "attachment_html" not in msgs[0]

    def test_skips_non_chat_events(self) -> None:
        # Events like checkpoint_result / tool_call / tool_result must not
        # produce chat bubbles — they live in the events table only.
        events = [
            _event("tool_call", {"tool": "write_file", "args": {"path": "x"}}),
            _event("tool_result", {"tool": "write_file", "ok": True}),
            _event(
                "checkpoint_result",
                {"name": "site_valid", "status": "pass", "criteria_results": {}},
            ),
            _event("alarm_raised", {"type": "tool_failed", "severity": "error"}),
        ]
        assert view_helpers.build_conversation(events, {}) == []

    def test_rewound_event_renders_as_divider(self) -> None:
        """A `rewound` event projects to a centered divider, not a bubble.

        Other roles must remain untouched (user/agent bubbles still build).
        """
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "ask_user",
                        "args": {"question": "Q?"},
                    }
                },
            ),
            _event(
                "rewound",
                {
                    "target_event_id": "0190a8d4-9c20-7000-8000-000000000aaa",
                    "removed_events": 3,
                    "removed_materials": 1,
                    "removed_checkpoints": 0,
                    "removed_alarms": 0,
                    "repended_material_id": "mat-q",
                },
            ),
        ]
        msgs = view_helpers.build_conversation(events, {})
        # First entry is the agent bubble; second is the divider.
        assert msgs[0]["role"] == "agent"
        assert msgs[1]["role"] == "divider"
        assert "Rewound" in msgs[1]["body"]
        assert "3" in msgs[1]["body"]
        assert "events removed" in msgs[1]["body"]


# ---------------------------------------------------------------------------
# Markdown rendering for agent bubbles
# ---------------------------------------------------------------------------


class TestMarkdownRendering:
    """Agent bubbles whose body comes from the LLM (final.summary,
    escalate.reason, ask_user.question, request_approval.subject) must be
    passed through the markdown renderer so `**bold**` and bullet lists
    look right in the chat panel. Internal summary strings (tool-call
    summaries we constructed) must NOT be markdown-rendered — they're
    already formatted. The raw text always remains available on ``body``
    for tests/debug. ``mistune`` is configured with ``escape=True`` so
    embedded HTML in untrusted LLM output cannot inject scripts.
    """

    def test_agent_bubble_for_final_renders_markdown(self) -> None:
        events = [
            _event(
                "worker_output",
                {"envelope": {"type": "final", "summary": "**Done!** Site shipped."}},
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert msgs[0]["body"] == "**Done!** Site shipped."
        assert "<strong>Done!</strong>" in msgs[0]["body_html"]

    def test_agent_bubble_for_escalate_renders_markdown(self) -> None:
        events = [
            _event(
                "worker_output",
                {"envelope": {"type": "escalate", "reason": "*blocked* on input"}},
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert "<em>blocked</em>" in msgs[0]["body_html"]
        assert msgs[0]["body"] == "*blocked* on input"

    def test_agent_bubble_for_ask_user_renders_markdown(self) -> None:
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "ask_user",
                        "args": {"question": "Which **theme** do you prefer?"},
                    }
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert "<strong>theme</strong>" in msgs[0]["body_html"]

    def test_agent_bubble_for_request_approval_renders_card_not_markdown(self) -> None:
        """Approval bubbles render a structured card, NOT a markdown
        paragraph. Subject text is escaped (no live ``**``-to-``<strong>``
        substitution) because the body is intentionally non-prose."""
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "request_approval",
                        "args": {"subject": "ship the **MVP** site"},
                    }
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        # Plain body summarises the approval for tests/debug.
        assert msgs[0]["body"].startswith("Approval request: ship the **MVP** site")
        # body_html carries an approval card — no markdown <strong> rendered.
        assert "approval-card" in msgs[0]["body_html"]
        assert "<strong>MVP</strong>" not in msgs[0]["body_html"]

    def test_agent_bubble_for_tool_call_summary_is_not_markdown_rendered(self) -> None:
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "write_file",
                        "args": {"path": "index.html", "content": "<html/>"},
                    }
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        # Internal summary string — must not be wrapped in a <p> by markdown
        # and must not contain rendered markdown elements. The plain body is
        # the one-line summary that tests/debug consume.
        assert msgs[0]["body"] == "Wrote index.html (7 bytes)"
        # body_html is the pre-rendered tool-call card — no markdown <p>
        # wrapping, no <strong>. Path appears in a <code>, byte count appears
        # in the summary line, content sits in a collapsed <details>.
        assert "<strong>" not in msgs[0]["body_html"]
        assert "tool-call-card" in msgs[0]["body_html"]
        assert "<code>index.html</code>" in msgs[0]["body_html"]
        assert "(7 bytes)" in msgs[0]["body_html"]

    def test_markdown_render_escapes_embedded_html(self) -> None:
        """LLM output is untrusted — a `<script>` token in the body MUST NOT
        survive as a live tag in body_html. mistune's escape=True handles this.

        Markdown around the escaped HTML still renders normally — we use a
        paragraph break so the **bold** span isn't fused into the same inline
        token as the (now-escaped) ``<script>``.
        """
        events = [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "final",
                        "summary": "<script>alert(1)</script>\n\n**bold**",
                    }
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        html_out = msgs[0]["body_html"]
        # No live <script> tag.
        assert "<script>" not in html_out
        assert "&lt;script&gt;" in html_out
        # **bold** still renders.
        assert "<strong>bold</strong>" in html_out

    def test_user_bubble_body_html_is_escaped_plain(self) -> None:
        """User bubbles are not markdown — plain text, HTML-escaped, newlines
        become <br>. A user typing `**not bold**` should see exactly that.
        """
        events = [
            _event(
                "human_resumed",
                {
                    "material_id": "ans",
                    "answer_or_decision": {"answer_text": "**not bold**\nline 2"},
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert msgs[0]["body"] == "**not bold**\nline 2"
        # Asterisks remain literal, newline becomes <br>, no markdown wrap.
        assert "**not bold**" in msgs[0]["body_html"]
        assert "<br>" in msgs[0]["body_html"]
        assert "<strong>" not in msgs[0]["body_html"]

    def test_user_bubble_escapes_html(self) -> None:
        events = [
            _event(
                "human_resumed",
                {
                    "material_id": "ans",
                    "answer_or_decision": {"answer_text": "<img src=x onerror=1>"},
                },
            )
        ]
        msgs = view_helpers.build_conversation(events, {})
        assert "<img" not in msgs[0]["body_html"]
        assert "&lt;img" in msgs[0]["body_html"]


# ---------------------------------------------------------------------------
# Approval card rendering (request_approval bubbles)
# ---------------------------------------------------------------------------


class TestApprovalCardRendering:
    """The ``request_approval`` chat bubble must NEVER show raw JSON or a
    ``<details>`` expander. ``subject == 'business_brief'`` and
    ``subject == 'mockup'`` get specific layouts; any other subject falls
    back to a labeled list (still no JSON). Every value derived from the
    agent's ``details`` dict is HTML-escaped because the body bypasses
    mistune — we emit HTML directly.
    """

    @staticmethod
    def _approval(subject: str, details: dict | None) -> list[dict]:
        return [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": "request_approval",
                        "args": {"subject": subject, "details": details},
                    }
                },
            )
        ]

    def test_business_brief_renders_as_card(self) -> None:
        events = self._approval(
            "business_brief",
            {
                "name": "Maria's",
                "industry": "restaurant",
                "phone": "555-0142",
                "pages": ["Home", "Menu"],
            },
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert '<div class="approval-card">' in body_html
        assert '<h3 class="approval-subject">Business Brief</h3>' in body_html
        # ``Maria's`` has an apostrophe — html.escape uses &#x27; for that.
        assert '<div class="brief-name">Maria&#x27;s</div>' in body_html
        assert "<dt>Industry</dt><dd>restaurant</dd>" in body_html
        assert "<dt>Pages</dt><dd>Home, Menu</dd>" in body_html
        # No raw JSON / details expander.
        assert "<pre>" not in body_html
        assert "<details" not in body_html
        # No JSON open-brace from a dumped dict.
        assert "{" not in body_html

    def test_business_brief_palette_renders_swatches(self) -> None:
        events = self._approval(
            "business_brief",
            {"palette": {"primary": "#7B1E1E", "secondary": "#F5E9DA"}},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert '<span class="swatch" style="background:#7B1E1E"' in body_html
        assert "<code>#7B1E1E</code>" in body_html
        assert '<span class="swatch" style="background:#F5E9DA"' in body_html
        assert "<code>#F5E9DA</code>" in body_html

    def test_business_brief_invalid_palette_color_does_not_render_swatch(self) -> None:
        events = self._approval(
            "business_brief",
            {"palette": {"primary": "javascript:alert(1)"}},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        # No inline style with a non-hex value.
        assert 'style="background:javascript' not in body_html
        # The dangerous text is at least escaped (no live `javascript:` href
        # because there's no ``<a>``; we only ever put it into <code>).
        assert "<code>" in body_html

    def test_business_brief_hours_dict_renders_human_labels(self) -> None:
        events = self._approval(
            "business_brief",
            {"hours": {"mon_thu": "11:00-21:00", "fri_sat": "11:00-22:00"}},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert "Mon–Thu" in body_html
        assert "Fri–Sat" in body_html
        assert "11:00-21:00" in body_html

    def test_business_brief_contact_dict_hoists_fields(self) -> None:
        events = self._approval(
            "business_brief",
            {"contact": {"phone": "555", "email": "x@y"}},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert "<dt>Phone</dt><dd>555</dd>" in body_html
        assert "<dt>Email</dt><dd>x@y</dd>" in body_html
        # No nested Contact sub-block (we hoisted phone/email).
        assert "<dt>Contact</dt>" not in body_html

    def test_business_brief_socials_dict_renders_inline(self) -> None:
        events = self._approval(
            "business_brief",
            {"socials": {"instagram": "@x", "twitter": "@y"}},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"].lower()
        assert "instagram: @x" in body_html
        assert "twitter: @y" in body_html

    def test_mockup_approval_renders_compact(self) -> None:
        events = self._approval(
            "mockup",
            {
                "sections": [{"name": "Header"}, {"name": "Hero"}],
                "primary_cta": "Reserve",
            },
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert '<h3 class="approval-subject">Mockup</h3>' in body_html
        assert "<dt>Sections</dt>" in body_html
        assert "Header, Hero" in body_html
        # No ASCII art — the prior render_mockup bubble already showed it.
        # We just confirm there's no large preformatted block in this bubble.
        assert "<pre>" not in body_html

    def test_unknown_subject_falls_back_to_labeled_list(self) -> None:
        events = self._approval(
            "shipping_terms",
            {"cutoff_time": "3pm", "free_threshold_usd": 50},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert '<h3 class="approval-subject">Shipping Terms</h3>' in body_html
        assert "<dt>Cutoff Time</dt><dd>3pm</dd>" in body_html
        assert "<dt>Free Threshold Usd</dt><dd>50</dd>" in body_html
        # No raw JSON.
        assert "<pre>" not in body_html
        assert "<details" not in body_html

    def test_approval_body_html_escapes_user_content(self) -> None:
        events = self._approval(
            "business_brief",
            {"name": "<script>alert(1)</script>"},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        # No live <script> tag in the rendered HTML.
        assert "<script>" not in body_html
        assert "&lt;script&gt;" in body_html

    def test_approval_no_details_renders_just_heading(self) -> None:
        for details in (None, {}):
            events = self._approval("business_brief", details)
            msgs = view_helpers.build_conversation(events, {})
            body_html = msgs[0]["body_html"]
            assert '<h3 class="approval-subject">Business Brief</h3>' in body_html
            assert 'class="approval-prompt"' in body_html
            # No crash, no JSON.
            assert "<pre>" not in body_html


# ---------------------------------------------------------------------------
# Per-tool tool_call bubble rendering — no JSON ever shown
# ---------------------------------------------------------------------------


class TestToolCallBubbleRendering:
    """Every ``tool_call`` envelope must produce a semantic bubble — never a
    JSON dump. ``save_business_brief`` reuses the Business-Brief card layout
    with a "Saving" heading; ``write_file`` is a compact card with the
    content tucked into a collapsed ``<details>``; ``read_file`` /
    ``list_files`` are one-liners; and any unknown tool falls back to a
    labeled-list generic card.
    """

    @staticmethod
    def _tool_call(tool: str, args: dict) -> list[dict]:
        return [
            _event(
                "worker_output",
                {
                    "envelope": {
                        "type": "tool_call",
                        "tool": tool,
                        "args": args,
                    }
                },
            )
        ]

    def test_save_business_brief_bubble_renders_as_card(self) -> None:
        brief = {
            "name": "Jerry's HVAC",
            "industry": "Home service",
            "palette": {"primary": "#1e40af", "secondary": "#ffffff"},
            "pages": ["Home", "Services"],
        }
        events = self._tool_call("save_business_brief", {"brief": brief})
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        # Heading distinguishes save from approve.
        assert '<h3 class="approval-subject">Saving Business Brief</h3>' in body_html
        # Brief name (escaped — apostrophe encoded).
        assert "Jerry&#x27;s HVAC" in body_html
        # Brief-rows definition list with at least one labeled row.
        assert '<dl class="brief-rows">' in body_html
        assert "<dt>Industry</dt><dd>Home service</dd>" in body_html
        assert "<dt>Pages</dt><dd>Home, Services</dd>" in body_html
        # Palette swatch with the primary color.
        assert 'style="background:#1e40af"' in body_html
        assert "<code>#1e40af</code>" in body_html
        # No JSON dump anywhere.
        assert "<pre>" not in body_html
        assert '{"' not in body_html
        assert '"brief":' not in body_html
        # Plain body reports the saved name.
        assert msgs[0]["body"] == "Saved Business Brief: Jerry's HVAC"

    def test_save_business_brief_unnamed_brief_still_renders(self) -> None:
        """A brief without ``name`` still renders the card and reports
        ``unnamed`` in the plain body so tests / debug get a stable string.
        """
        events = self._tool_call(
            "save_business_brief", {"brief": {"industry": "Construction"}}
        )
        msgs = view_helpers.build_conversation(events, {})
        assert msgs[0]["body"] == "Saved Business Brief: unnamed"
        assert (
            '<h3 class="approval-subject">Saving Business Brief</h3>'
            in msgs[0]["body_html"]
        )

    def test_save_business_brief_escapes_user_content(self) -> None:
        """User-supplied values (here a name) must be HTML-escaped — a
        ``<script>`` token cannot survive as a live tag in body_html.
        """
        events = self._tool_call(
            "save_business_brief",
            {"brief": {"name": "<script>alert(1)</script>"}},
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert "<script>" not in body_html
        assert "&lt;script&gt;" in body_html
        # Plain body carries the raw name; that's fine because tests/debug
        # don't render it as HTML.
        assert "Saved Business Brief: <script>alert(1)</script>" == msgs[0]["body"]

    def test_write_file_bubble_compact_with_collapsed_content(self) -> None:
        long_content = "<html>...</html>" * 100  # 1600 chars > 500 truncation
        events = self._tool_call(
            "write_file", {"path": "index.html", "content": long_content}
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        # Path is bolded via <code> and byte count appears in the summary line.
        assert "<code>index.html</code>" in body_html
        assert f"({len(long_content)} bytes)" in body_html
        # The content is tucked into a collapsed <details>.
        assert "<details" in body_html
        assert "<summary>Show content</summary>" in body_html
        # Full content is NOT inlined — only the first 500 chars are shown,
        # followed by a "... (N more characters)" footer (all escaped).
        assert long_content not in body_html
        assert "more characters)" in body_html
        # The pre block shows escaped HTML, not live HTML.
        assert "&lt;html&gt;" in body_html
        # Plain body summary.
        assert msgs[0]["body"] == f"Wrote index.html ({len(long_content)} bytes)"

    def test_write_file_short_content_inlined_without_truncation_footer(self) -> None:
        events = self._tool_call(
            "write_file", {"path": "small.txt", "content": "hello"}
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        # Whole content fits — no truncation footer.
        assert "more characters)" not in body_html
        # Content is present, escaped (no HTML special chars here so just text).
        assert "hello" in body_html

    def test_read_file_bubble_compact(self) -> None:
        events = self._tool_call("read_file", {"path": "src/index.html"})
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert "tool-read-file" in body_html
        assert "Read <code>src/index.html</code>" in body_html
        # No JSON.
        assert "<pre>" not in body_html
        assert '{"' not in body_html
        assert msgs[0]["body"] == "Read src/index.html"

    def test_list_files_bubble_compact(self) -> None:
        events = self._tool_call("list_files", {"path": "src"})
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert "Listed files under <code>src</code>" in body_html
        assert "<pre>" not in body_html
        assert '{"' not in body_html
        assert msgs[0]["body"] == "Listed files under src"

    def test_list_files_default_path_when_unset(self) -> None:
        """Omitting ``path`` falls back to ``.`` (the sandbox root)."""
        events = self._tool_call("list_files", {})
        msgs = view_helpers.build_conversation(events, {})
        assert "Listed files under <code>.</code>" in msgs[0]["body_html"]
        assert msgs[0]["body"] == "Listed files under ."

    def test_generic_tool_call_renders_card_not_json(self) -> None:
        """A fictional tool with no explicit branch must still render a
        semantic card — heading + labeled rows — not a JSON blob.
        """
        events = self._tool_call("do_thing", {"a": 1, "b": "two"})
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        # Heading is title-cased (underscores → spaces, then Title Case).
        assert '<h3 class="tool-call-heading">Do Thing</h3>' in body_html
        # Labeled rows for each arg.
        assert "<dt>A</dt><dd>1</dd>" in body_html
        assert "<dt>B</dt><dd>two</dd>" in body_html
        # No JSON shape — neither raw braces nor JSON-style keys.
        assert "{" not in body_html
        assert '"a":' not in body_html
        assert "<pre>" not in body_html
        # Plain body reports the call but stays JSON-free.
        assert msgs[0]["body"] == "Call do_thing"
        assert "{" not in msgs[0]["body"]

    def test_generic_tool_call_escapes_arg_values(self) -> None:
        """Generic-card arg values must be HTML-escaped before insertion."""
        events = self._tool_call(
            "future_tool", {"payload": "<script>alert(1)</script>"}
        )
        msgs = view_helpers.build_conversation(events, {})
        body_html = msgs[0]["body_html"]
        assert "<script>" not in body_html
        assert "&lt;script&gt;" in body_html


# ---------------------------------------------------------------------------
# derive_active_models
# ---------------------------------------------------------------------------


class TestDeriveActiveModels:
    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_CHAT", "chat-primary")
        monkeypatch.setenv("MODEL_CHAT_FALLBACK", "chat-fallback")
        monkeypatch.setenv("MODEL_CODE", "code-primary")
        monkeypatch.setenv("MODEL_CODE_FALLBACK", "code-fallback")
        out = view_helpers.derive_active_models()
        assert out == {
            "chat": {"primary": "chat-primary", "fallback": "chat-fallback"},
            "code": {"primary": "code-primary", "fallback": "code-fallback"},
        }

    def test_empty_fallback_renders_dash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_CHAT", "x")
        monkeypatch.delenv("MODEL_CHAT_FALLBACK", raising=False)
        monkeypatch.setenv("MODEL_CODE", "y")
        monkeypatch.delenv("MODEL_CODE_FALLBACK", raising=False)
        out = view_helpers.derive_active_models()
        assert out["chat"]["fallback"] == "—"
        assert out["code"]["fallback"] == "—"

    def test_all_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in (
            "MODEL_CHAT",
            "MODEL_CHAT_FALLBACK",
            "MODEL_CODE",
            "MODEL_CODE_FALLBACK",
        ):
            monkeypatch.delenv(k, raising=False)
        out = view_helpers.derive_active_models()
        assert out["chat"]["primary"] == "—"
        assert out["code"]["primary"] == "—"
