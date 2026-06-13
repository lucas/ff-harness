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


def test_latest_material_by_type_returns_most_recent(tmp_session):
    _, session_conn, _ = tmp_session
    # Write three materials of the same type; ids are UUID7 so insertion
    # order matches `id DESC` order.
    a = store.persist_material(session_conn, "out", "bootstrap", "business_brief", {"name": "first"})
    b = store.persist_material(session_conn, "out", "bootstrap", "business_brief", {"name": "second"})
    c = store.persist_material(session_conn, "out", "bootstrap", "business_brief", {"name": "third"})
    # And a noise row of a different type.
    store.persist_material(session_conn, "out", "build", "site_file", {"path": "x"})

    latest = store.latest_material_by_type(session_conn, "business_brief")
    assert latest is not None
    assert latest["id"] == c
    assert latest["content"] == {"name": "third"}

    # Distinct ids exercised, no off-by-one.
    assert a != b != c


def test_latest_material_by_type_missing_returns_none(tmp_session):
    _, session_conn, _ = tmp_session
    assert store.latest_material_by_type(session_conn, "business_brief") is None


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


# ---------------------------------------------------------------------------
# rewind_to_awaiting_human — destructive truncation by UUID7 id
# ---------------------------------------------------------------------------


def _build_rewind_fixture(session_conn: sqlite3.Connection) -> dict:
    """Build a fixture session resembling: ask_user(A), awaiting_human(A),
    human_resumed(A), worker_output(ask_user_2), awaiting_human(B),
    human_resumed(B), plus a checkpoint and an alarm after the first pause.

    Returns a dict of ids keyed for clarity in test assertions.
    """
    # Material A — the first ask_user's pending question (gets re-pended).
    material_a = store.persist_material(
        session_conn, "out", "bootstrap", "pending_question",
        {"question": "Name?"}, pending=False,  # resolved when user answered
    )
    awaiting_a_event = store.append_event(
        session_conn, "awaiting_human", "bootstrap",
        {"material_id": material_a, "reason": "ask_user"},
        material_id=material_a,
    )
    # User's answer material + the human_resumed event referencing it.
    answer_a = store.persist_material(
        session_conn, "in", "bootstrap", "user_answer",
        {"answer_text": "Maria's"},
    )
    human_resumed_a = store.append_event(
        session_conn, "human_resumed", "bootstrap",
        {"material_id": answer_a, "answer_or_decision": {"answer_text": "Maria's"}},
        material_id=answer_a,
    )
    # Next worker turn writes worker_output + a checkpoint and an alarm.
    worker_output = store.append_event(
        session_conn, "worker_output", "bootstrap",
        {"envelope": {"type": "tool_call", "tool": "ask_user", "args": {}}},
    )
    checkpoint_after = store.persist_checkpoint(
        session_conn, "business_brief_confirmed", "bootstrap", "pass", {"x": True},
    )
    alarm_after = store.persist_alarm(
        session_conn, "tool_failed", "error", {"tool": "x"}, "act", "bootstrap",
    )
    # Second ask_user -> pending material B + awaiting_human(B) event.
    material_b = store.persist_material(
        session_conn, "out", "bootstrap", "pending_question",
        {"question": "Industry?"}, pending=False,
    )
    awaiting_b_event = store.append_event(
        session_conn, "awaiting_human", "bootstrap",
        {"material_id": material_b, "reason": "ask_user"},
        material_id=material_b,
    )
    answer_b = store.persist_material(
        session_conn, "in", "bootstrap", "user_answer",
        {"answer_text": "Restaurant"},
    )
    human_resumed_b = store.append_event(
        session_conn, "human_resumed", "bootstrap",
        {"material_id": answer_b, "answer_or_decision": {"answer_text": "Restaurant"}},
        material_id=answer_b,
    )
    return {
        "material_a": material_a,
        "awaiting_a_event": awaiting_a_event,
        "answer_a": answer_a,
        "human_resumed_a": human_resumed_a,
        "worker_output": worker_output,
        "checkpoint_after": checkpoint_after,
        "alarm_after": alarm_after,
        "material_b": material_b,
        "awaiting_b_event": awaiting_b_event,
        "answer_b": answer_b,
        "human_resumed_b": human_resumed_b,
    }


def test_rewind_to_awaiting_human_deletes_and_repends(tmp_session):
    _, session_conn, _ = tmp_session
    ids = _build_rewind_fixture(session_conn)

    report = store.rewind_to_awaiting_human(session_conn, ids["awaiting_a_event"])

    # Events after the rewind target are gone (six of them: human_resumed_a,
    # worker_output, awaiting_b_event, human_resumed_b — checkpoints/alarms
    # are NOT events so they're counted separately).
    assert report["removed_events"] == 4
    # Materials after the target: answer_a, material_b, answer_b = 3.
    assert report["removed_materials"] == 3
    assert report["removed_checkpoints"] == 1
    assert report["removed_alarms"] == 1
    assert report["target_event_id"] == ids["awaiting_a_event"]
    assert report["repended_material_id"] == ids["material_a"]
    assert isinstance(report["rewind_event_id"], str)

    # Concrete state: material A is back to pending=1; material B is gone.
    a = store.load_material(session_conn, ids["material_a"])
    assert a is not None and a["pending"] is True
    assert store.load_material(session_conn, ids["material_b"]) is None
    assert store.load_material(session_conn, ids["answer_a"]) is None
    assert store.load_material(session_conn, ids["answer_b"]) is None

    # Events list: ends with the new `rewound` event AFTER the target.
    events = store.load_events(session_conn)
    types = [e["type"] for e in events]
    assert types[-1] == "rewound"
    # The events before the rewound entry must NOT include any post-target event.
    surviving_ids = [e["id"] for e in events if e["type"] != "rewound"]
    assert ids["awaiting_a_event"] in surviving_ids
    assert ids["human_resumed_a"] not in surviving_ids
    assert ids["worker_output"] not in surviving_ids
    assert ids["awaiting_b_event"] not in surviving_ids
    # The rewind event's payload carries the report counts.
    rewound_payload = events[-1]["payload"]
    assert rewound_payload["target_event_id"] == ids["awaiting_a_event"]
    assert rewound_payload["removed_events"] == 4
    assert rewound_payload["repended_material_id"] == ids["material_a"]


def test_rewind_target_must_be_awaiting_human(tmp_session):
    _, session_conn, _ = tmp_session
    worker_output_id = store.append_event(
        session_conn, "worker_output", "bootstrap",
        {"envelope": {"type": "final", "summary": "ok"}},
    )
    with pytest.raises(ValueError):
        store.rewind_to_awaiting_human(session_conn, worker_output_id)


def test_rewind_target_must_exist(tmp_session):
    _, session_conn, _ = tmp_session
    with pytest.raises(ValueError):
        store.rewind_to_awaiting_human(session_conn, "00000000-0000-7000-8000-000000000000")


def test_rewind_appends_rewound_event_with_correct_payload(tmp_session):
    _, session_conn, _ = tmp_session
    ids = _build_rewind_fixture(session_conn)
    report = store.rewind_to_awaiting_human(session_conn, ids["awaiting_a_event"])

    events = store.load_events(session_conn)
    rewound = next(e for e in events if e["type"] == "rewound")
    assert rewound["id"] == report["rewind_event_id"]
    # Stage is taken from the target event (bootstrap in the fixture).
    assert rewound["stage"] == "bootstrap"
    expected_keys = {
        "target_event_id",
        "removed_events",
        "removed_materials",
        "removed_checkpoints",
        "removed_alarms",
        "repended_material_id",
    }
    assert set(rewound["payload"].keys()) == expected_keys


# ---------------------------------------------------------------------------
# llm_calls — full LLM call audit log (per-session DB)
# ---------------------------------------------------------------------------


def _sample_messages() -> list[dict]:
    return [
        {"role": "system", "content": "you are a website-building agent"},
        {"role": "user", "content": "Make me a homepage."},
    ]


def _sample_options() -> dict:
    return {"response_format": {"type": "json_object"}, "temperature": 0.2}


def test_record_llm_call_roundtrip(tmp_session):
    _, session_conn, _ = tmp_session
    cid = store.record_llm_call(
        session_conn,
        model="deepseek/deepseek-v4-flash:free",
        is_fallback=False,
        request_messages=_sample_messages(),
        request_options=_sample_options(),
        response_text='{"type":"final","summary":"ok"}',
        finish_reason="stop",
        tokens_in=12,
        tokens_out=8,
        cost_usd=0.0,
        status="ok",
    )
    assert uuid.UUID(cid).version == 7

    rows = store.load_llm_calls(session_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == cid
    assert row["model"] == "deepseek/deepseek-v4-flash:free"
    assert row["is_fallback"] is False
    # JSON columns are decoded back to Python.
    assert row["request_messages"] == _sample_messages()
    assert row["request_options"] == _sample_options()
    assert row["response_text"] == '{"type":"final","summary":"ok"}'
    assert row["finish_reason"] == "stop"
    assert row["tokens_in"] == 12
    assert row["tokens_out"] == 8
    assert row["cost_usd"] == 0.0
    assert row["status"] == "ok"
    assert row["error_message"] is None
    assert row["related_event_id"] is None
    assert row["related_material_id"] is None


def test_record_llm_call_rejects_invalid_status(tmp_session):
    _, session_conn, _ = tmp_session
    with pytest.raises(ValueError):
        store.record_llm_call(
            session_conn,
            model="m",
            is_fallback=False,
            request_messages=_sample_messages(),
            request_options=None,
            response_text=None,
            finish_reason=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status="bogus_status",
        )


def test_record_llm_call_fk_enforced(tmp_session):
    _, session_conn, _ = tmp_session
    bogus = new_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.record_llm_call(
            session_conn,
            model="m",
            is_fallback=False,
            request_messages=_sample_messages(),
            request_options=None,
            response_text=None,
            finish_reason=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status="ok",
            related_event_id=bogus,
        )


def test_record_llm_call_links_to_existing_event(tmp_session):
    """The related_event_id FK accepts a real worker_input event id."""
    _, session_conn, _ = tmp_session
    eid = store.append_event(
        session_conn, "worker_input", "bootstrap",
        {"messages_count": 2, "tokens_estimate": 100},
    )
    cid = store.record_llm_call(
        session_conn,
        model="m",
        is_fallback=False,
        request_messages=_sample_messages(),
        request_options=None,
        response_text="x",
        finish_reason=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        status="ok",
        related_event_id=eid,
    )
    rows = store.load_llm_calls(session_conn)
    assert rows[0]["id"] == cid
    assert rows[0]["related_event_id"] == eid


def test_load_llm_calls_chronological(tmp_session):
    _, session_conn, _ = tmp_session
    ids = [
        store.record_llm_call(
            session_conn,
            model=f"m{i}",
            is_fallback=False,
            request_messages=_sample_messages(),
            request_options=None,
            response_text="x",
            finish_reason=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status="ok",
        )
        for i in range(3)
    ]
    rows = store.load_llm_calls(session_conn)
    assert [r["id"] for r in rows] == ids


def test_load_llm_calls_with_limit_returns_most_recent(tmp_session):
    _, session_conn, _ = tmp_session
    ids = [
        store.record_llm_call(
            session_conn,
            model=f"m{i}",
            is_fallback=False,
            request_messages=_sample_messages(),
            request_options=None,
            response_text="x",
            finish_reason=None,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            status="ok",
        )
        for i in range(5)
    ]
    rows = store.load_llm_calls(session_conn, limit=2)
    # The two MOST RECENT, still in chronological (ASC) order.
    assert [r["id"] for r in rows] == ids[-2:]


def test_latest_worker_input_event_id_returns_latest(tmp_session):
    _, session_conn, _ = tmp_session
    store.append_event(session_conn, "worker_output", "bootstrap", {})
    e1 = store.append_event(session_conn, "worker_input", "bootstrap", {"i": 1})
    store.append_event(session_conn, "tool_call", "bootstrap", {})
    e2 = store.append_event(session_conn, "worker_input", "bootstrap", {"i": 2})
    assert store.latest_worker_input_event_id(session_conn) == e2
    assert e1 != e2


def test_latest_worker_input_event_id_returns_none_when_no_events(tmp_session):
    _, session_conn, _ = tmp_session
    assert store.latest_worker_input_event_id(session_conn) is None


def test_rewind_inside_transaction_no_partial_delete_on_error(tmp_session, monkeypatch):
    """A failure mid-transaction must roll back all deletes + the re-pend."""
    _, session_conn, _ = tmp_session
    ids = _build_rewind_fixture(session_conn)
    snapshot_events_before = len(store.load_events(session_conn))
    snapshot_materials_before = session_conn.execute(
        "SELECT COUNT(*) AS n FROM material"
    ).fetchone()["n"]

    # Force json.dumps to raise during the rewound-event INSERT so the SQL
    # inside the `with session_conn:` block aborts. The `with` context
    # manager on a sqlite3 connection rolls back the transaction on
    # exception, leaving every prior DELETE + UPDATE in the block reverted.
    import harness.services.store as store_mod

    original_dumps = store_mod.json.dumps
    call_count = {"n": 0}

    def flaky_dumps(*args, **kwargs):
        # The rewind helper calls json.dumps exactly once (for the new
        # rewound event's payload). Skip nothing; just fail every call —
        # the first call inside the helper happens AFTER the deletes and
        # the UPDATE, so a failure there exercises the rollback path.
        call_count["n"] += 1
        raise RuntimeError("simulated failure during audit-event insert")

    monkeypatch.setattr(store_mod.json, "dumps", flaky_dumps)

    with pytest.raises(RuntimeError, match="simulated failure"):
        store.rewind_to_awaiting_human(session_conn, ids["awaiting_a_event"])

    # Restore so subsequent assertions can read.
    monkeypatch.setattr(store_mod.json, "dumps", original_dumps)

    # DB is unchanged: same event count, same material count, material A
    # is still resolved (pending=0).
    assert len(store.load_events(session_conn)) == snapshot_events_before
    assert (
        session_conn.execute("SELECT COUNT(*) AS n FROM material").fetchone()["n"]
        == snapshot_materials_before
    )
    a = store.load_material(session_conn, ids["material_a"])
    assert a is not None and a["pending"] is False
