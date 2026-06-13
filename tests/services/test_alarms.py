from __future__ import annotations

from harness.models.enums import AlarmType, EventType, Severity
from harness.services import alarms, store


def _load_single_alarm(session_conn) -> dict:
    rows = store.load_alarms(session_conn)
    assert len(rows) == 1, f"expected exactly 1 alarm, got {len(rows)}"
    return rows[0]


def _load_alarm_raised_event(session_conn, alarm_id: str) -> dict:
    events = [
        e for e in store.load_events(session_conn)
        if e["type"] == EventType.ALARM_RAISED.value
    ]
    matches = [e for e in events if e["alarm_id"] == alarm_id]
    assert len(matches) == 1, (
        f"expected exactly 1 alarm_raised event for alarm {alarm_id},"
        f" got {len(matches)}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# Convenience constructors — one test per named alarm.
# ---------------------------------------------------------------------------


def test_raise_iteration_limit_reached(tmp_session):
    _core, session_conn, _sid = tmp_session
    alarm_id = alarms.raise_iteration_limit_reached(
        session_conn,
        iter_count=10,
        last_checkpoint="mockup_approved",
        stage="build",
    )
    row = _load_single_alarm(session_conn)
    assert row["id"] == alarm_id
    assert row["type"] == AlarmType.ITERATION_LIMIT_REACHED.value
    assert row["severity"] == Severity.WARNING.value
    assert row["stage"] == "build"
    assert row["context"] == {
        "iter_count": 10,
        "last_checkpoint": "mockup_approved",
        "stage": "build",
    }
    assert "10" in row["recommended_action"]
    assert "build" in row["recommended_action"]

    event = _load_alarm_raised_event(session_conn, alarm_id)
    assert event["payload"] == {
        "alarm_id": alarm_id,
        "type": AlarmType.ITERATION_LIMIT_REACHED.value,
        "severity": Severity.WARNING.value,
    }
    assert row["triggered_by_event_id"] == event["id"]


def test_raise_spend_cap_reached(tmp_session):
    _core, session_conn, _sid = tmp_session
    alarm_id = alarms.raise_spend_cap_reached(
        session_conn,
        spent_usd=1.25,
        cap_usd=1.0,
        stage="build",
    )
    row = _load_single_alarm(session_conn)
    assert row["id"] == alarm_id
    assert row["type"] == AlarmType.SPEND_CAP_REACHED.value
    assert row["severity"] == Severity.CRITICAL.value
    assert row["stage"] == "build"
    assert row["context"] == {
        "spent_usd": 1.25,
        "cap_usd": 1.0,
        "window": "day",
    }
    assert "1.25" in row["recommended_action"]
    assert "1.0" in row["recommended_action"]
    assert "day" in row["recommended_action"]

    event = _load_alarm_raised_event(session_conn, alarm_id)
    assert event["payload"]["alarm_id"] == alarm_id
    assert event["payload"]["type"] == AlarmType.SPEND_CAP_REACHED.value
    assert event["payload"]["severity"] == Severity.CRITICAL.value
    assert row["triggered_by_event_id"] == event["id"]


def test_raise_output_schema_violation(tmp_session):
    _core, session_conn, _sid = tmp_session
    alarm_id = alarms.raise_output_schema_violation(
        session_conn,
        parse_error="invalid JSON at line 3",
        repair_attempt=2,
        raw_text_preview="{'tool': 'write_file' but truncated...",
        stage="mockup",
    )
    row = _load_single_alarm(session_conn)
    assert row["id"] == alarm_id
    assert row["type"] == AlarmType.OUTPUT_SCHEMA_VIOLATION.value
    assert row["severity"] == Severity.CRITICAL.value
    assert row["stage"] == "mockup"
    assert row["context"] == {
        "parse_error": "invalid JSON at line 3",
        "repair_attempt": 2,
        "raw_text_preview": "{'tool': 'write_file' but truncated...",
    }
    assert "2" in row["recommended_action"]

    event = _load_alarm_raised_event(session_conn, alarm_id)
    assert event["payload"]["alarm_id"] == alarm_id
    assert event["stage"] == "mockup"
    assert row["triggered_by_event_id"] == event["id"]


def test_raise_tool_failed(tmp_session):
    _core, session_conn, _sid = tmp_session
    alarm_id = alarms.raise_tool_failed(
        session_conn,
        tool="write_file",
        args={"path": "../escape.txt", "content": "x"},
        error_kind="path_outside_sandbox",
        error_message="path resolves outside sandbox_root",
        stage="build",
    )
    row = _load_single_alarm(session_conn)
    assert row["id"] == alarm_id
    assert row["type"] == AlarmType.TOOL_FAILED.value
    assert row["severity"] == Severity.ERROR.value
    assert row["stage"] == "build"
    assert row["context"] == {
        "tool": "write_file",
        "args": {"path": "../escape.txt", "content": "x"},
        "error_kind": "path_outside_sandbox",
        "error_message": "path resolves outside sandbox_root",
    }
    assert "write_file" in row["recommended_action"]
    assert "path_outside_sandbox" in row["recommended_action"]

    event = _load_alarm_raised_event(session_conn, alarm_id)
    assert event["payload"]["alarm_id"] == alarm_id
    assert event["payload"]["type"] == AlarmType.TOOL_FAILED.value
    assert event["payload"]["severity"] == Severity.ERROR.value
    assert row["triggered_by_event_id"] == event["id"]


# ---------------------------------------------------------------------------
# Generic raise_alarm path — proves enum-or-string ergonomics and that a
# caller can supply a custom recommended_action.
# ---------------------------------------------------------------------------


def test_raise_alarm_generic_with_string_type(tmp_session):
    _core, session_conn, _sid = tmp_session
    alarm_id = alarms.raise_alarm(
        session_conn,
        type="tool_failed",
        severity="error",
        context={
            "tool": "render_mockup",
            "args": {"layout_spec": {}},
            "error_kind": "empty_spec",
            "error_message": "layout_spec has no sections",
        },
        recommended_action="Retry render_mockup with a populated layout_spec.",
        stage="mockup",
    )
    row = _load_single_alarm(session_conn)
    assert row["id"] == alarm_id
    assert row["type"] == "tool_failed"
    assert row["severity"] == "error"
    assert row["recommended_action"] == (
        "Retry render_mockup with a populated layout_spec."
    )
    assert row["stage"] == "mockup"
    assert row["context"]["tool"] == "render_mockup"

    event = _load_alarm_raised_event(session_conn, alarm_id)
    assert event["payload"]["alarm_id"] == alarm_id
    assert row["triggered_by_event_id"] == event["id"]
