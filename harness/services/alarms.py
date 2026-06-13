"""Alarm raising: persists an alarms row, appends an alarm_raised event,
and back-links the alarm to that event.

This module is the SOLE caller of store.persist_alarm and the SOLE emitter
of EventType.ALARM_RAISED events. Centralising the choreography here keeps
the back-link contract (alarms.triggered_by_event_id <-> events.id) intact.
"""

from __future__ import annotations

import sqlite3
from enum import Enum

from harness.models.enums import AlarmType, EventType, Severity
from harness.services import store


# Verbatim recommended_action templates from docs/v1-spec.md § The 4 alarms.
_ITERATION_LIMIT_REACHED_TMPL = (
    "Pause for human approval; agent has iterated {iter_count} times"
    " without approval at stage {stage}."
)
_SPEND_CAP_REACHED_TMPL = (
    "Halt all worker calls; spent ${spent_usd} of ${cap_usd} cap for"
    " window '{window}'. Wait for window reset or raise cap."
)
_OUTPUT_SCHEMA_VIOLATION_TMPL = (
    "Repair attempted {repair_attempt}x and still invalid. Stop and surface"
    " raw_text_preview to human."
)
_TOOL_FAILED_TMPL = (
    "Tool {tool} failed with {error_kind}. Inspect args; consider retry or"
    " alternative tool."
)


def _coerce(value: object) -> str:
    # str-Enum members are also str instances, but in Python 3.11+ str(member)
    # returns "EnumName.MEMBER". Use .value to get the canonical on-disk string.
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, str):
        return value
    raise TypeError(f"expected str or str-Enum, got {type(value).__name__}")


def raise_alarm(
    session_conn: sqlite3.Connection,
    *,
    type: AlarmType | str,
    severity: Severity | str,
    context: dict,
    recommended_action: str,
    stage: str,
) -> str:
    """Persist the alarm row, append an alarm_raised event, set back-link.

    Returns the alarm id. The event payload is {alarm_id, type, severity}.
    """
    type_str = _coerce(type)
    severity_str = _coerce(severity)

    alarm_id = store.persist_alarm(
        session_conn,
        type_str,
        severity_str,
        context,
        recommended_action,
        stage,
    )
    event_id = store.append_event(
        session_conn,
        type=EventType.ALARM_RAISED.value,
        stage=stage,
        payload={
            "alarm_id": alarm_id,
            "type": type_str,
            "severity": severity_str,
        },
        alarm_id=alarm_id,
    )
    store.set_alarm_triggered_by(session_conn, alarm_id, event_id)
    return alarm_id


# ---------------------------------------------------------------------------
# Convenience constructors for the 4 named alarms.
# Each enforces the spec-defined severity and recommended-action template
# and assembles the type-specific context shape.
# ---------------------------------------------------------------------------


def raise_iteration_limit_reached(
    session_conn: sqlite3.Connection,
    *,
    iter_count: int,
    last_checkpoint: str | None,
    stage: str,
) -> str:
    return raise_alarm(
        session_conn,
        type=AlarmType.ITERATION_LIMIT_REACHED,
        severity=Severity.WARNING,
        context={
            "iter_count": iter_count,
            "last_checkpoint": last_checkpoint,
            "stage": stage,
        },
        recommended_action=_ITERATION_LIMIT_REACHED_TMPL.format(
            iter_count=iter_count, stage=stage
        ),
        stage=stage,
    )


def raise_spend_cap_reached(
    session_conn: sqlite3.Connection,
    *,
    spent_usd: float,
    cap_usd: float,
    stage: str,
) -> str:
    window = "day"
    return raise_alarm(
        session_conn,
        type=AlarmType.SPEND_CAP_REACHED,
        severity=Severity.CRITICAL,
        context={
            "spent_usd": spent_usd,
            "cap_usd": cap_usd,
            "window": window,
        },
        recommended_action=_SPEND_CAP_REACHED_TMPL.format(
            spent_usd=spent_usd, cap_usd=cap_usd, window=window
        ),
        stage=stage,
    )


def raise_output_schema_violation(
    session_conn: sqlite3.Connection,
    *,
    parse_error: str,
    repair_attempt: int,
    raw_text_preview: str,
    stage: str,
) -> str:
    return raise_alarm(
        session_conn,
        type=AlarmType.OUTPUT_SCHEMA_VIOLATION,
        severity=Severity.CRITICAL,
        context={
            "parse_error": parse_error,
            "repair_attempt": repair_attempt,
            "raw_text_preview": raw_text_preview,
        },
        recommended_action=_OUTPUT_SCHEMA_VIOLATION_TMPL.format(
            repair_attempt=repair_attempt
        ),
        stage=stage,
    )


def raise_tool_failed(
    session_conn: sqlite3.Connection,
    *,
    tool: str,
    args: dict,
    error_kind: str,
    error_message: str,
    stage: str,
) -> str:
    return raise_alarm(
        session_conn,
        type=AlarmType.TOOL_FAILED,
        severity=Severity.ERROR,
        context={
            "tool": tool,
            "args": args,
            "error_kind": error_kind,
            "error_message": error_message,
        },
        recommended_action=_TOOL_FAILED_TMPL.format(
            tool=tool, error_kind=error_kind
        ),
        stage=stage,
    )
