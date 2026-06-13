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
        # request_approval bubble = "Requesting approval: <subject>".
        assert msgs[2]["body"].startswith("Requesting approval: mockup")
        assert "details_json" in msgs[2]["meta"]
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
