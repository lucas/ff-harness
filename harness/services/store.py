"""Raw sqlite3 persistence layer for the two-tier harness store.

All SQL lives here. No business logic, no HTTP, no LLM calls. Every function
takes a connection as the first argument; opening and lifecycle are the
caller's responsibility (see core_connection / session_connection helpers).

Conventions:
- All inserts mint ids via harness.models.ids.new_id (UUID7, TEXT-stored).
- All queries are parameterized.
- JSON columns (payload, content, context, criteria_results) are dict-in,
  dict-out: encoded with json.dumps on insert, decoded on read.
- Timestamps default to datetime.now(UTC).isoformat() but accept an injected
  ts for deterministic testing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.models.ddl import (
    CORE_DDL,
    PRAGMA_FOREIGN_KEYS,
    SESSION_DDL,
)
from harness.models.ids import new_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_ts(ts: datetime | str | None) -> str:
    if ts is None:
        return _now_iso()
    if isinstance(ts, datetime):
        return ts.isoformat()
    return ts


def _open(path: Path, ddl: list[str]) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # PRAGMA must be set per-connection; ON-clause has no effect inside a tx.
    conn.execute(PRAGMA_FOREIGN_KEYS)
    for stmt in ddl:
        conn.execute(stmt)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def core_connection(db_path: Path) -> sqlite3.Connection:
    return _open(Path(db_path), CORE_DDL)


def session_connection(sessions_dir: Path, session_id: str) -> sqlite3.Connection:
    path = Path(sessions_dir) / f"{session_id}.db"
    return _open(path, SESSION_DDL)


def init_core(db_path: Path) -> None:
    conn = core_connection(Path(db_path))
    conn.close()


def init_session(sessions_dir: Path, session_id: str) -> None:
    conn = session_connection(Path(sessions_dir), session_id)
    conn.close()


# ---------------------------------------------------------------------------
# Sessions (core DB)
# ---------------------------------------------------------------------------


def create_session(
    core_conn: sqlite3.Connection,
    current_stage: str = "bootstrap",
    *,
    ts: datetime | str | None = None,
) -> str:
    sid = new_id()
    now = _resolve_ts(ts)
    core_conn.execute(
        "INSERT INTO sessions (id, status, current_stage, iter_since_approval, created_at, updated_at)"
        " VALUES (?, ?, ?, 0, ?, ?)",
        (sid, "active", current_stage, now, now),
    )
    core_conn.commit()
    return sid


def update_session_status(
    core_conn: sqlite3.Connection,
    session_id: str,
    status: str,
    *,
    current_stage: str | None = None,
    iter_since_approval: int | None = None,
    ts: datetime | str | None = None,
) -> None:
    fields = ["status = ?", "updated_at = ?"]
    params: list[object] = [status, _resolve_ts(ts)]
    if current_stage is not None:
        fields.append("current_stage = ?")
        params.append(current_stage)
    if iter_since_approval is not None:
        fields.append("iter_since_approval = ?")
        params.append(iter_since_approval)
    params.append(session_id)
    # Field list is built from a closed set of literal column names, never user input.
    sql = "UPDATE sessions SET " + ", ".join(fields) + " WHERE id = ?"
    core_conn.execute(sql, params)
    core_conn.commit()


def load_session(core_conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = core_conn.execute(
        "SELECT id, status, current_stage, iter_since_approval, created_at, updated_at"
        " FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def list_sessions(core_conn: sqlite3.Connection) -> list[dict]:
    rows = core_conn.execute(
        "SELECT id, status, current_stage, iter_since_approval, created_at, updated_at"
        " FROM sessions ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Spend (core DB)
# ---------------------------------------------------------------------------


def record_spend(
    core_conn: sqlite3.Connection,
    session_id: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    *,
    is_fallback: bool = False,
    ts: datetime | str | None = None,
) -> str:
    sid = new_id()
    core_conn.execute(
        "INSERT INTO spend_log (id, ts, session_id, model, is_fallback, tokens_in, tokens_out, cost_usd)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            _resolve_ts(ts),
            session_id,
            model,
            1 if is_fallback else 0,
            tokens_in,
            tokens_out,
            cost_usd,
        ),
    )
    core_conn.commit()
    return sid


def recent_spend_today_usd(
    core_conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> float:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()
    row = core_conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_log WHERE ts >= ?",
        (cutoff,),
    ).fetchone()
    return float(row["total"])


def spend_summary_for_session(
    core_conn: sqlite3.Connection,
    session_id: str,
) -> dict:
    """Aggregate spend rows for a single session.

    Returns `{total_usd, by_model: {model_string: total_usd}, fallback_count}`.
    Used by GET /sessions/{id} per docs/http-api.md. Sessions with no spend
    return `{total_usd: 0.0, by_model: {}, fallback_count: 0}`.
    """
    rows = core_conn.execute(
        "SELECT model, is_fallback, cost_usd FROM spend_log WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    total_usd = 0.0
    by_model: dict[str, float] = {}
    fallback_count = 0
    for r in rows:
        cost = float(r["cost_usd"])
        total_usd += cost
        model = str(r["model"])
        by_model[model] = by_model.get(model, 0.0) + cost
        if int(r["is_fallback"]) == 1:
            fallback_count += 1
    return {
        "total_usd": total_usd,
        "by_model": by_model,
        "fallback_count": fallback_count,
    }


# ---------------------------------------------------------------------------
# Events (per-session DB)
# ---------------------------------------------------------------------------


def append_event(
    session_conn: sqlite3.Connection,
    type: str,
    stage: str,
    payload: dict,
    *,
    material_id: str | None = None,
    checkpoint_id: str | None = None,
    alarm_id: str | None = None,
    ts: datetime | str | None = None,
) -> str:
    eid = new_id()
    session_conn.execute(
        "INSERT INTO events (id, ts, type, stage, payload, material_id, checkpoint_id, alarm_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            _resolve_ts(ts),
            type,
            stage,
            json.dumps(payload),
            material_id,
            checkpoint_id,
            alarm_id,
        ),
    )
    session_conn.commit()
    return eid


def load_events(
    session_conn: sqlite3.Connection,
    *,
    since_id: str | None = None,
) -> list[dict]:
    if since_id is None:
        rows = session_conn.execute(
            "SELECT id, ts, type, stage, payload, material_id, checkpoint_id, alarm_id"
            " FROM events ORDER BY id ASC"
        ).fetchall()
    else:
        rows = session_conn.execute(
            "SELECT id, ts, type, stage, payload, material_id, checkpoint_id, alarm_id"
            " FROM events WHERE id > ? ORDER BY id ASC",
            (since_id,),
        ).fetchall()
    return [_decode_event(r) for r in rows]


def _decode_event(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def rewind_to_awaiting_human(
    session_conn: sqlite3.Connection,
    target_event_id: str,
) -> dict:
    """Truncate per-session DB rows whose id > target_event_id.

    Validates that the target event exists and is an ``awaiting_human`` event;
    otherwise raises :class:`ValueError`. Re-pends the associated pending
    material (the one the session was paused on), then appends a single
    ``rewound`` audit event so the truncation is visible in the event log.

    All deletes + the re-pend + the audit insert happen inside one transaction
    so a partial state is impossible. The orchestrator (NOT this function)
    is responsible for updating the core ``sessions`` row (status, stage,
    iter_since_approval) to match the rewound state.

    Returns a report dict::

        {
            "target_event_id": str,
            "target_event_payload": dict,
            "removed_events": int,
            "removed_materials": int,
            "removed_checkpoints": int,
            "removed_alarms": int,
            "repended_material_id": str | None,
            "rewind_event_id": str,
        }
    """
    target_row = session_conn.execute(
        "SELECT id, ts, type, stage, payload, material_id, checkpoint_id, alarm_id"
        " FROM events WHERE id = ?",
        (target_event_id,),
    ).fetchone()
    if target_row is None:
        raise ValueError(
            "target event not found or not an awaiting_human event"
        )
    if target_row["type"] != "awaiting_human":
        raise ValueError(
            "target event not found or not an awaiting_human event"
        )
    target_payload = json.loads(target_row["payload"])
    target_stage = target_row["stage"]
    pending_material_id = (
        target_payload.get("material_id")
        if isinstance(target_payload, dict)
        else None
    )

    # Single-transaction truncation. The `with` block on a sqlite3.Connection
    # commits on success and rolls back on exception, so an error inside the
    # body (e.g. FK violation, monkeypatched failure) leaves the DB unchanged.
    with session_conn:
        # Count then delete. Order is FK-safe: events references material /
        # checkpoints / alarms, so events must die first (deleting those
        # parents would otherwise trip FK enforcement).
        removed_events = int(
            session_conn.execute(
                "SELECT COUNT(*) FROM events WHERE id > ?",
                (target_event_id,),
            ).fetchone()[0]
        )
        removed_alarms = int(
            session_conn.execute(
                "SELECT COUNT(*) FROM alarms WHERE id > ?",
                (target_event_id,),
            ).fetchone()[0]
        )
        removed_checkpoints = int(
            session_conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE id > ?",
                (target_event_id,),
            ).fetchone()[0]
        )
        removed_materials = int(
            session_conn.execute(
                "SELECT COUNT(*) FROM material WHERE id > ?",
                (target_event_id,),
            ).fetchone()[0]
        )
        session_conn.execute(
            "DELETE FROM events WHERE id > ?", (target_event_id,)
        )
        session_conn.execute(
            "DELETE FROM alarms WHERE id > ?", (target_event_id,)
        )
        session_conn.execute(
            "DELETE FROM checkpoints WHERE id > ?", (target_event_id,)
        )
        session_conn.execute(
            "DELETE FROM material WHERE id > ?", (target_event_id,)
        )

        # Re-pend the material the session was paused on. The deletes above
        # cannot have removed it (its id is <= target_event_id since the
        # awaiting_human event references a material that existed at pause
        # time), but guard in case the row was already gone for any reason.
        repended_material_id: str | None = None
        if isinstance(pending_material_id, str):
            row = session_conn.execute(
                "SELECT id FROM material WHERE id = ?",
                (pending_material_id,),
            ).fetchone()
            if row is not None:
                session_conn.execute(
                    "UPDATE material SET pending = 1 WHERE id = ?",
                    (pending_material_id,),
                )
                repended_material_id = pending_material_id

        # Append the audit event AFTER the deletes so the new event sorts
        # after the rewind target (UUID7 is time-ordered, the new id is fresh).
        rewind_event_id = new_id()
        session_conn.execute(
            "INSERT INTO events (id, ts, type, stage, payload, material_id, checkpoint_id, alarm_id)"
            " VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (
                rewind_event_id,
                _now_iso(),
                "rewound",
                target_stage,
                json.dumps(
                    {
                        "target_event_id": target_event_id,
                        "removed_events": removed_events,
                        "removed_materials": removed_materials,
                        "removed_checkpoints": removed_checkpoints,
                        "removed_alarms": removed_alarms,
                        "repended_material_id": repended_material_id,
                    }
                ),
            ),
        )

    return {
        "target_event_id": target_event_id,
        "target_event_payload": target_payload
        if isinstance(target_payload, dict)
        else {},
        "removed_events": removed_events,
        "removed_materials": removed_materials,
        "removed_checkpoints": removed_checkpoints,
        "removed_alarms": removed_alarms,
        "repended_material_id": repended_material_id,
        "rewind_event_id": rewind_event_id,
    }


# ---------------------------------------------------------------------------
# Material (per-session DB)
# ---------------------------------------------------------------------------


def persist_material(
    session_conn: sqlite3.Connection,
    direction: str,
    stage: str,
    type: str,
    content: dict,
    *,
    pending: bool = False,
    ts: datetime | str | None = None,
) -> str:
    mid = new_id()
    session_conn.execute(
        "INSERT INTO material (id, direction, stage, type, content, pending, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            mid,
            direction,
            stage,
            type,
            json.dumps(content),
            1 if pending else 0,
            _resolve_ts(ts),
        ),
    )
    session_conn.commit()
    return mid


def load_pending_materials(session_conn: sqlite3.Connection) -> list[dict]:
    rows = session_conn.execute(
        "SELECT id, direction, stage, type, content, pending, created_at"
        " FROM material WHERE pending = 1 ORDER BY id ASC"
    ).fetchall()
    return [_decode_material(r) for r in rows]


def mark_material_resolved(session_conn: sqlite3.Connection, material_id: str) -> None:
    session_conn.execute(
        "UPDATE material SET pending = 0 WHERE id = ?",
        (material_id,),
    )
    session_conn.commit()


def load_material(session_conn: sqlite3.Connection, material_id: str) -> dict | None:
    row = session_conn.execute(
        "SELECT id, direction, stage, type, content, pending, created_at"
        " FROM material WHERE id = ?",
        (material_id,),
    ).fetchone()
    return _decode_material(row) if row else None


def latest_material_by_type(
    session_conn: sqlite3.Connection, type: str
) -> dict | None:
    """Return the most recent material of a given type, or None.

    Ordering by `id DESC` works because UUID7 ids are time-ordered (first
    48 bits are a millisecond timestamp). Used by tools that need to read
    the most recent persisted material of a known type (e.g. `render_mockup`
    looking up the latest `business_brief` for theming).
    """
    row = session_conn.execute(
        "SELECT id, direction, stage, type, content, pending, created_at"
        " FROM material WHERE type = ? ORDER BY id DESC LIMIT 1",
        (type,),
    ).fetchone()
    return _decode_material(row) if row else None


def _decode_material(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["content"] = json.loads(d["content"])
    d["pending"] = bool(d["pending"])
    return d


# ---------------------------------------------------------------------------
# Checkpoints (per-session DB)
# ---------------------------------------------------------------------------


def persist_checkpoint(
    session_conn: sqlite3.Connection,
    name: str,
    stage: str,
    status: str,
    criteria_results: dict,
    *,
    material_id: str | None = None,
    ts: datetime | str | None = None,
) -> str:
    cid = new_id()
    session_conn.execute(
        "INSERT INTO checkpoints (id, name, stage, status, criteria_results, material_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            cid,
            name,
            stage,
            status,
            json.dumps(criteria_results),
            material_id,
            _resolve_ts(ts),
        ),
    )
    session_conn.commit()
    return cid


def load_checkpoints(
    session_conn: sqlite3.Connection,
    *,
    name: str | None = None,
) -> list[dict]:
    if name is None:
        rows = session_conn.execute(
            "SELECT id, name, stage, status, criteria_results, material_id, created_at"
            " FROM checkpoints ORDER BY created_at ASC"
        ).fetchall()
    else:
        rows = session_conn.execute(
            "SELECT id, name, stage, status, criteria_results, material_id, created_at"
            " FROM checkpoints WHERE name = ? ORDER BY created_at ASC",
            (name,),
        ).fetchall()
    return [_decode_checkpoint(r) for r in rows]


def _decode_checkpoint(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["criteria_results"] = json.loads(d["criteria_results"])
    return d


# ---------------------------------------------------------------------------
# Alarms (per-session DB)
# ---------------------------------------------------------------------------


def persist_alarm(
    session_conn: sqlite3.Connection,
    type: str,
    severity: str,
    context: dict,
    recommended_action: str,
    stage: str,
    *,
    ts: datetime | str | None = None,
) -> str:
    aid = new_id()
    session_conn.execute(
        "INSERT INTO alarms (id, type, severity, context, recommended_action, stage,"
        " triggered_by_event_id, resolved, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?)",
        (
            aid,
            type,
            severity,
            json.dumps(context),
            recommended_action,
            stage,
            _resolve_ts(ts),
        ),
    )
    session_conn.commit()
    return aid


def set_alarm_triggered_by(
    session_conn: sqlite3.Connection,
    alarm_id: str,
    event_id: str,
) -> None:
    session_conn.execute(
        "UPDATE alarms SET triggered_by_event_id = ? WHERE id = ?",
        (event_id, alarm_id),
    )
    session_conn.commit()


def load_alarms(
    session_conn: sqlite3.Connection,
    *,
    resolved: bool | None = None,
) -> list[dict]:
    if resolved is None:
        rows = session_conn.execute(
            "SELECT id, type, severity, context, recommended_action, stage,"
            " triggered_by_event_id, resolved, created_at"
            " FROM alarms ORDER BY created_at ASC"
        ).fetchall()
    else:
        rows = session_conn.execute(
            "SELECT id, type, severity, context, recommended_action, stage,"
            " triggered_by_event_id, resolved, created_at"
            " FROM alarms WHERE resolved = ? ORDER BY created_at ASC",
            (1 if resolved else 0,),
        ).fetchall()
    return [_decode_alarm(r) for r in rows]


def mark_alarm_resolved(session_conn: sqlite3.Connection, alarm_id: str) -> None:
    session_conn.execute(
        "UPDATE alarms SET resolved = 1 WHERE id = ?",
        (alarm_id,),
    )
    session_conn.commit()


def _decode_alarm(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["context"] = json.loads(d["context"])
    d["resolved"] = bool(d["resolved"])
    return d


# ---------------------------------------------------------------------------
# LLM calls (per-session DB) — full request/response audit log
# ---------------------------------------------------------------------------
#
# Relationship to `spend_log` (core DB):
#   - spend_log is the lightweight cross-session rolling-cap ledger; the
#     `recent_spend_today_usd` query runs every turn, so it stays lean.
#   - llm_calls is the per-call detail log: full request messages, full
#     response text, FK links to the per-session `events` / `material` rows.
#     Volume / payload size makes it a per-session-DB concern.
#   - Both rows are written on every successful API attempt. We deliberately
#     do NOT try to merge them.

_LLM_CALL_STATUSES: frozenset[str] = frozenset(
    {
        "ok",
        "rate_limited",
        "transport_error",
        "parse_error",
        "repair_retry",
        "language_violation",
    }
)


def record_llm_call(
    session_conn: sqlite3.Connection,
    *,
    model: str,
    is_fallback: bool,
    request_messages: list[dict],
    request_options: dict | None,
    response_text: str | None,
    finish_reason: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    status: str,
    error_message: str | None = None,
    related_event_id: str | None = None,
    related_material_id: str | None = None,
    ts: datetime | str | None = None,
) -> str:
    """Persist one LLM call attempt to the per-session ``llm_calls`` table.

    JSON-encodes ``request_messages`` and ``request_options``. Validates
    ``status`` against the closed set ``{ok, rate_limited, transport_error,
    parse_error, repair_retry, language_violation}`` so a typo can't quietly
    land in the table. Returns the new row's id.
    """
    if status not in _LLM_CALL_STATUSES:
        raise ValueError(
            f"invalid llm_calls status {status!r};"
            f" expected one of {sorted(_LLM_CALL_STATUSES)}"
        )
    call_id = new_id()
    now = _resolve_ts(ts)
    session_conn.execute(
        "INSERT INTO llm_calls ("
        " id, ts, model, is_fallback, request_messages, request_options,"
        " response_text, finish_reason, tokens_in, tokens_out, cost_usd,"
        " status, error_message, related_event_id, related_material_id,"
        " created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_id,
            now,
            model,
            1 if is_fallback else 0,
            json.dumps(request_messages),
            json.dumps(request_options) if request_options is not None else None,
            response_text,
            finish_reason,
            int(tokens_in),
            int(tokens_out),
            float(cost_usd),
            status,
            error_message,
            related_event_id,
            related_material_id,
            now,
        ),
    )
    session_conn.commit()
    return call_id


def load_llm_calls(
    session_conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    since_id: str | None = None,
) -> list[dict]:
    """Return llm_calls rows ordered by id ASC (chronological, UUID7).

    Decodes ``request_messages`` / ``request_options`` JSON back to Python
    objects so callers get usable structures. When ``limit`` is provided,
    returns the most recent N rows (still in chronological order — the
    template renders newest at the bottom to match the chat panel).
    """
    if since_id is not None:
        rows = session_conn.execute(
            "SELECT id, ts, model, is_fallback, request_messages, request_options,"
            " response_text, finish_reason, tokens_in, tokens_out, cost_usd,"
            " status, error_message, related_event_id, related_material_id,"
            " created_at FROM llm_calls WHERE id > ? ORDER BY id ASC",
            (since_id,),
        ).fetchall()
    elif limit is not None:
        # "Most recent N, chronological": fetch N by DESC then reverse.
        rows_desc = session_conn.execute(
            "SELECT id, ts, model, is_fallback, request_messages, request_options,"
            " response_text, finish_reason, tokens_in, tokens_out, cost_usd,"
            " status, error_message, related_event_id, related_material_id,"
            " created_at FROM llm_calls ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        rows = list(reversed(rows_desc))
    else:
        rows = session_conn.execute(
            "SELECT id, ts, model, is_fallback, request_messages, request_options,"
            " response_text, finish_reason, tokens_in, tokens_out, cost_usd,"
            " status, error_message, related_event_id, related_material_id,"
            " created_at FROM llm_calls ORDER BY id ASC"
        ).fetchall()
    return [_decode_llm_call(r) for r in rows]


def _decode_llm_call(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["is_fallback"] = bool(d["is_fallback"])
    raw_messages = d.get("request_messages")
    d["request_messages"] = (
        json.loads(raw_messages) if isinstance(raw_messages, str) else []
    )
    raw_options = d.get("request_options")
    d["request_options"] = (
        json.loads(raw_options) if isinstance(raw_options, str) else None
    )
    return d


def latest_worker_input_event_id(
    session_conn: sqlite3.Connection,
) -> str | None:
    """Return the most recent ``worker_input`` event id, or None.

    Used by LLMWorker to link each ``llm_calls`` row to the turn-triggering
    event so the UI can group calls by turn. UUID7 ordering = chronological.
    """
    row = session_conn.execute(
        "SELECT id FROM events WHERE type = 'worker_input'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return str(row["id"])
