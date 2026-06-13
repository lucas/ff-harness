from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from harness.models.ids import new_id
from harness.services import store


# ---------------------------------------------------------------------------
# Core DB: sessions + spend_log
# ---------------------------------------------------------------------------


def test_session_roundtrip(tmp_core_conn):
    sid = store.create_session(tmp_core_conn, current_stage="bootstrap")
    assert uuid.UUID(sid).version == 7

    loaded = store.load_session(tmp_core_conn, sid)
    assert loaded is not None
    assert loaded["id"] == sid
    assert loaded["status"] == "active"
    assert loaded["current_stage"] == "bootstrap"
    assert loaded["iter_since_approval"] == 0
    assert loaded["created_at"] == loaded["updated_at"]


def test_update_session_status_and_stage(tmp_core_conn):
    sid = store.create_session(tmp_core_conn)
    store.update_session_status(
        tmp_core_conn,
        sid,
        "awaiting_human",
        current_stage="mockup",
        iter_since_approval=4,
    )
    loaded = store.load_session(tmp_core_conn, sid)
    assert loaded is not None
    assert loaded["status"] == "awaiting_human"
    assert loaded["current_stage"] == "mockup"
    assert loaded["iter_since_approval"] == 4


def test_list_sessions_most_recent_first(tmp_core_conn):
    a = store.create_session(tmp_core_conn)
    b = store.create_session(tmp_core_conn)
    c = store.create_session(tmp_core_conn)
    rows = store.list_sessions(tmp_core_conn)
    assert [r["id"] for r in rows] == [c, b, a]


def test_load_session_missing_returns_none(tmp_core_conn):
    assert store.load_session(tmp_core_conn, "nope-not-a-real-id") is None


def test_spend_roundtrip_and_rolling_window(tmp_core_conn):
    sid = store.create_session(tmp_core_conn)
    now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    inside_1 = now - timedelta(hours=1)
    inside_2 = now - timedelta(hours=23)
    outside_1 = now - timedelta(hours=25)
    outside_2 = now - timedelta(days=3)

    store.record_spend(tmp_core_conn, sid, "deepseek/deepseek-v4-flash:free", 10, 20, 0.0, ts=inside_1)
    store.record_spend(tmp_core_conn, sid, "deepseek/deepseek-v4-flash", 11, 22, 0.05, is_fallback=True, ts=inside_2)
    store.record_spend(tmp_core_conn, sid, "qwen/qwen3-coder:free", 12, 24, 0.0, ts=outside_1)
    store.record_spend(tmp_core_conn, sid, "qwen/qwen3-coder", 13, 26, 0.25, is_fallback=True, ts=outside_2)

    total = store.recent_spend_today_usd(tmp_core_conn, now=now)
    assert total == pytest.approx(0.05)


def test_recent_spend_today_zero_when_empty(tmp_core_conn):
    now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    assert store.recent_spend_today_usd(tmp_core_conn, now=now) == 0.0


def test_recent_spend_today_aggregates_across_sessions(tmp_core_conn):
    sid_a = store.create_session(tmp_core_conn)
    sid_b = store.create_session(tmp_core_conn)
    now = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    store.record_spend(tmp_core_conn, sid_a, "m1", 1, 1, 0.10, ts=now - timedelta(hours=2))
    store.record_spend(tmp_core_conn, sid_b, "m2", 1, 1, 0.30, is_fallback=True, ts=now - timedelta(hours=4))
    store.record_spend(tmp_core_conn, sid_b, "m2", 1, 1, 0.99, ts=now - timedelta(hours=30))
    assert store.recent_spend_today_usd(tmp_core_conn, now=now) == pytest.approx(0.40)


def test_spend_fk_enforced_against_sessions(tmp_core_conn):
    with pytest.raises(sqlite3.IntegrityError):
        store.record_spend(tmp_core_conn, "nonexistent-session", "m", 1, 1, 0.01)


# ---------------------------------------------------------------------------
# Per-session DB: material / checkpoints / alarms / events roundtrip + FKs
# ---------------------------------------------------------------------------


def test_material_roundtrip(tmp_session):
    _, session_conn, _ = tmp_session
    content = {"question": "What is the business name?", "options": ["a", "b"]}
    mid = store.persist_material(
        session_conn, "out", "bootstrap", "pending_question", content, pending=True
    )
    assert uuid.UUID(mid).version == 7

    loaded = store.load_material(session_conn, mid)
    assert loaded is not None
    assert loaded["id"] == mid
    assert loaded["direction"] == "out"
    assert loaded["stage"] == "bootstrap"
    assert loaded["type"] == "pending_question"
    assert loaded["content"] == content
    assert loaded["pending"] is True

    pending = store.load_pending_materials(session_conn)
    assert [m["id"] for m in pending] == [mid]

    store.mark_material_resolved(session_conn, mid)
    assert store.load_pending_materials(session_conn) == []
    resolved = store.load_material(session_conn, mid)
    assert resolved is not None
    assert resolved["pending"] is False


def test_load_pending_materials_oldest_first(tmp_session):
    _, session_conn, _ = tmp_session
    a = store.persist_material(session_conn, "out", "bootstrap", "pending_question", {"q": "a"}, pending=True)
    b = store.persist_material(session_conn, "out", "bootstrap", "pending_question", {"q": "b"}, pending=True)
    c = store.persist_material(session_conn, "out", "bootstrap", "pending_question", {"q": "c"}, pending=True)
    pending = store.load_pending_materials(session_conn)
    assert [m["id"] for m in pending] == [a, b, c]


def test_checkpoint_roundtrip_with_material_fk(tmp_session):
    _, session_conn, _ = tmp_session
    mid = store.persist_material(session_conn, "out", "build", "site_file", {"path": "index.html", "bytes": 42})
    criteria = {"html5_parses": True, "has_title": False}
    cid = store.persist_checkpoint(
        session_conn, "site_valid", "build", "fail", criteria, material_id=mid
    )
    rows = store.load_checkpoints(session_conn, name="site_valid")
    assert len(rows) == 1
    assert rows[0]["id"] == cid
    assert rows[0]["status"] == "fail"
    assert rows[0]["criteria_results"] == criteria
    assert rows[0]["material_id"] == mid


def test_load_checkpoints_ordered_by_created_at(tmp_session):
    _, session_conn, _ = tmp_session
    a = store.persist_checkpoint(session_conn, "site_valid", "build", "fail", {"x": False})
    b = store.persist_checkpoint(session_conn, "site_valid", "build", "pass", {"x": True})
    rows = store.load_checkpoints(session_conn)
    assert [r["id"] for r in rows] == [a, b]


def test_alarm_roundtrip_and_resolve(tmp_session):
    _, session_conn, _ = tmp_session
    ctx = {"tool": "write_file", "args": {"path": "/etc/passwd"}, "error_kind": "sandbox_escape", "error_message": "denied"}
    aid = store.persist_alarm(
        session_conn, "tool_failed", "error", ctx, "Inspect args", "build"
    )
    rows = store.load_alarms(session_conn)
    assert len(rows) == 1
    assert rows[0]["id"] == aid
    assert rows[0]["context"] == ctx
    assert rows[0]["severity"] == "error"
    assert rows[0]["triggered_by_event_id"] is None
    assert rows[0]["resolved"] is False

    # Resolved filter
    assert len(store.load_alarms(session_conn, resolved=False)) == 1
    assert len(store.load_alarms(session_conn, resolved=True)) == 0

    store.mark_alarm_resolved(session_conn, aid)
    assert store.load_alarms(session_conn, resolved=True)[0]["id"] == aid


def test_set_alarm_triggered_by_event(tmp_session):
    _, session_conn, _ = tmp_session
    aid = store.persist_alarm(session_conn, "tool_failed", "error", {"k": "v"}, "do thing", "build")
    eid = store.append_event(session_conn, "alarm_raised", "build", {"alarm_id": aid, "type": "tool_failed", "severity": "error"}, alarm_id=aid)
    store.set_alarm_triggered_by(session_conn, aid, eid)
    rows = store.load_alarms(session_conn)
    assert rows[0]["triggered_by_event_id"] == eid


def test_event_roundtrip_with_all_fk_columns(tmp_session):
    _, session_conn, _ = tmp_session
    mid = store.persist_material(session_conn, "out", "build", "site_file", {"path": "p"})
    cid = store.persist_checkpoint(session_conn, "site_valid", "build", "pass", {"x": True}, material_id=mid)
    aid = store.persist_alarm(session_conn, "tool_failed", "error", {"e": 1}, "act", "build")

    payload = {"tool": "write_file", "ok": True, "result_or_error": {"path": "p", "bytes": 12}}
    eid = store.append_event(
        session_conn,
        "tool_result",
        "build",
        payload,
        material_id=mid,
        checkpoint_id=cid,
        alarm_id=aid,
    )
    events = store.load_events(session_conn)
    assert len(events) == 1
    row = events[0]
    assert row["id"] == eid
    assert row["type"] == "tool_result"
    assert row["stage"] == "build"
    assert row["payload"] == payload
    assert row["material_id"] == mid
    assert row["checkpoint_id"] == cid
    assert row["alarm_id"] == aid


def test_events_ordered_by_id_matches_insertion(tmp_session):
    _, session_conn, _ = tmp_session
    ids = [
        store.append_event(session_conn, "worker_input", "bootstrap", {"i": i})
        for i in range(10)
    ]
    rows = store.load_events(session_conn)
    assert [r["id"] for r in rows] == ids
    assert [r["payload"]["i"] for r in rows] == list(range(10))


def test_load_events_since_id(tmp_session):
    _, session_conn, _ = tmp_session
    ids = [store.append_event(session_conn, "worker_input", "bootstrap", {"i": i}) for i in range(5)]
    after = store.load_events(session_conn, since_id=ids[2])
    assert [r["id"] for r in after] == ids[3:]


# ---------------------------------------------------------------------------
# FK enforcement (PRAGMA foreign_keys = ON on every connection)
# ---------------------------------------------------------------------------


def test_event_material_id_fk_enforced(tmp_session):
    _, session_conn, _ = tmp_session
    bogus = new_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.append_event(session_conn, "worker_input", "build", {}, material_id=bogus)


def test_event_checkpoint_id_fk_enforced(tmp_session):
    _, session_conn, _ = tmp_session
    bogus = new_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.append_event(session_conn, "checkpoint_result", "build", {}, checkpoint_id=bogus)


def test_event_alarm_id_fk_enforced(tmp_session):
    _, session_conn, _ = tmp_session
    bogus = new_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.append_event(session_conn, "alarm_raised", "build", {}, alarm_id=bogus)


def test_checkpoint_material_id_fk_enforced(tmp_session):
    _, session_conn, _ = tmp_session
    bogus = new_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.persist_checkpoint(
            session_conn, "site_valid", "build", "pass", {"x": True}, material_id=bogus
        )


# ---------------------------------------------------------------------------
# SQL-injection-shape input must be parameterized safely (no execution).
# ---------------------------------------------------------------------------


def test_parameterized_against_injection_shape(tmp_session):
    _, session_conn, _ = tmp_session
    nasty = "'); DROP TABLE material; --"
    mid = store.persist_material(session_conn, "out", "bootstrap", "user_answer", {"answer_text": nasty})
    loaded = store.load_material(session_conn, mid)
    assert loaded is not None
    assert loaded["content"]["answer_text"] == nasty
    # Table still exists; this would raise OperationalError if dropped.
    assert store.load_pending_materials(session_conn) == []
