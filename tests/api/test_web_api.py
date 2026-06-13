"""Step 9 gate — FastAPI TestClient drives the 5 HTTP routes.

Strategy:
  - Override `AppContext.worker_for_stage_factory` so every stage gets the
    same MockWorker (configured per-test). No real LLM calls.
  - Build an isolated AppContext under tmp_path; the test owns the
    filesystem so concurrent tests don't trip over each other.

These tests exercise the JSON shapes in docs/http-api.md; any drift between
that doc and the route output should produce a failing assertion here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.api.app import create_app
from harness.api.dependencies import AppContext
from harness.models.envelope import Escalate, Final, ToolCall
from harness.services import store
from harness.services.llm import OpenRouterClient
from harness.services.worker import MockWorker, Worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app_context(
    *,
    data_dir: Path,
    mock_worker: MockWorker,
) -> AppContext:
    """Build an AppContext that hands the SAME MockWorker to every stage.

    The `worker_for_stage_factory` signature mirrors
    `domain.make_worker_for_stage`; we ignore its kwargs and return a closure
    that yields the supplied mock for any stage string.
    """
    sessions_dir = data_dir / "sessions"
    sites_dir = data_dir / "sites"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sites_dir.mkdir(parents=True, exist_ok=True)

    def factory(**_kwargs) -> Callable[[str], Worker]:
        def worker_for_stage(_stage: str) -> Worker:
            return mock_worker

        return worker_for_stage

    return AppContext(
        core_db_path=data_dir / "harness.db",
        sessions_dir=sessions_dir,
        sites_dir=sites_dir,
        # OpenRouterClient is never called in MockWorker mode; provide an
        # instance so AppContext stays a normal dataclass.
        llm_client=OpenRouterClient(api_key="test-key-not-used"),
        worker_for_stage_factory=factory,
    )


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[[list], TestClient]]:
    """Yield a factory that takes a scripted-response list and returns a TestClient.

    Cleans up nothing explicitly — tmp_path is per-test, so file handles in
    the TestClient go away with the GC pass at the end of the test.
    """

    def _make(scripted_responses: list[ToolCall | Final | Escalate]) -> TestClient:
        worker = MockWorker(scripted_responses=scripted_responses)
        ctx = _make_test_app_context(
            data_dir=tmp_path / "data", mock_worker=worker
        )
        app = create_app(ctx)
        return TestClient(app)

    yield _make


# ---------------------------------------------------------------------------
# 1. Create + list
# ---------------------------------------------------------------------------


def test_create_session_and_list(make_client):
    client = make_client([])
    r = client.post("/sessions", json={})
    assert r.status_code == 201
    body = r.json()
    assert set(body.keys()) == {
        "session_id",
        "status",
        "current_stage",
        "created_at",
    }
    assert body["status"] == "active"
    assert body["current_stage"] == "bootstrap"
    sid = body["session_id"]
    assert isinstance(sid, str) and len(sid) >= 32

    r2 = client.get("/sessions")
    assert r2.status_code == 200
    listed = r2.json()
    assert "sessions" in listed
    ids = [s["id"] for s in listed["sessions"]]
    assert sid in ids
    # Newest first ordering (UUID7 sort): only one session here so trivially
    # holds, but we assert the row shape on the head.
    head = listed["sessions"][0]
    for field in (
        "id",
        "status",
        "current_stage",
        "iter_since_approval",
        "created_at",
        "updated_at",
    ):
        assert field in head


# ---------------------------------------------------------------------------
# 2. Detail on a fresh session
# ---------------------------------------------------------------------------


def test_get_detail_on_fresh_session(make_client):
    client = make_client([])
    sid = client.post("/sessions", json={}).json()["session_id"]

    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()

    # Top-level keys per docs/http-api.md.
    assert set(body.keys()) == {
        "session",
        "events",
        "checkpoints",
        "alarms",
        "pending_materials",
        "spend_summary",
        "llm_calls",
    }

    # Session row shape.
    assert body["session"]["id"] == sid
    assert body["session"]["status"] == "active"
    assert body["session"]["current_stage"] == "bootstrap"

    # Fresh session — no events, no checkpoints, no alarms, no pending
    # materials. The session starts empty; the bootstrap flow populates it.
    assert body["events"] == []
    assert body["checkpoints"] == []
    assert body["alarms"] == []
    assert body["pending_materials"] == []

    # Spend summary shape: empty.
    assert body["spend_summary"] == {
        "total_usd": 0.0,
        "by_model": {},
        "fallback_count": 0,
    }


# ---------------------------------------------------------------------------
# 3. Resume -> pause -> answer -> resume -> final
# ---------------------------------------------------------------------------


_GOOD_HTML = (
    "<!DOCTYPE html>"
    '<html lang="en">'
    '<head><meta name="viewport" content="width=device-width">'
    "<title>Maria's Pizzeria</title></head>"
    "<body><h1>Welcome to Maria's</h1>"
    "<p>The best wood-fired pizza in town.</p>"
    "</body></html>"
)


def _restaurant_short_script() -> list[ToolCall | Final | Escalate]:
    """Compact scripted flow: approval -> render_mockup -> write_file -> final."""
    return [
        ToolCall(
            tool="request_approval",
            args={"subject": "mockup", "details": {"note": "ready"}},
        ),
        ToolCall(
            tool="render_mockup",
            args={
                "layout_spec": {
                    "sections": [
                        {"name": "Header"},
                        {"name": "Hero"},
                        {"name": "Menu"},
                    ],
                    "primary_cta": "Reserve",
                }
            },
        ),
        ToolCall(
            tool="write_file",
            args={"path": "index.html", "content": _GOOD_HTML},
        ),
        Final(summary="done."),
    ]


def test_resume_pause_answer_resume_final(make_client):
    client = make_client(_restaurant_short_script())
    sid = client.post("/sessions", json={}).json()["session_id"]

    # First resume: hits request_approval -> awaiting_human.
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "session_id",
        "status",
        "current_stage",
        "terminal",
        "paused_reason",
    }
    assert body["status"] == "awaiting_human"
    assert body["paused_reason"] == "awaiting_human"
    assert body["terminal"] is False

    # Find the pending material to answer against.
    detail = client.get(f"/sessions/{sid}").json()
    pending = detail["pending_materials"]
    assert len(pending) == 1
    pending_id = pending[0]["id"]
    assert pending[0]["content"]["kind"] == "approval"

    # Approve the mockup. This single call also runs the loop forward.
    r2 = client.post(
        f"/sessions/{sid}/answer",
        json={
            "material_id": pending_id,
            "approved": True,
            "notes": "looks great",
        },
    )
    assert r2.status_code == 200
    final_body = r2.json()
    assert final_body["status"] == "completed"
    assert final_body["terminal"] is True
    assert final_body["paused_reason"] is None
    assert final_body["current_stage"] == "done"

    # Inspect: checkpoints + alarms.
    detail2 = client.get(f"/sessions/{sid}").json()
    assert detail2["session"]["status"] == "completed"
    # All write_file post-hooks ran; we expect at least site_valid + seo
    # checkpoint rows.
    ckpt_names = {c["name"] for c in detail2["checkpoints"]}
    assert "site_valid" in ckpt_names
    assert "seo_artifacts_present" in ckpt_names
    # The mockup approval flow generated mockup_renders + mockup_approved.
    assert "mockup_renders" in ckpt_names
    assert "mockup_approved" in ckpt_names
    # No alarms in the happy path.
    assert detail2["alarms"] == []


# ---------------------------------------------------------------------------
# 4. Answer-then-resume in one call
# ---------------------------------------------------------------------------


def test_answer_endpoint_runs_the_loop(make_client):
    """The /answer endpoint must drive run_until_pause itself (one HTTP call).

    Scripted: approval -> final. Approving the question should produce a
    terminal RunResult right out of the /answer response.
    """
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(
            tool="ask_user",
            args={"question": "What is your business name?"},
        ),
        Final(summary="captured the name."),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    # Resume pauses on ask_user.
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.json()["paused_reason"] == "awaiting_human"

    detail = client.get(f"/sessions/{sid}").json()
    pending_id = detail["pending_materials"][0]["id"]
    # ask_user content has no "kind" field, so it's treated as a question.
    assert detail["pending_materials"][0]["content"].get("kind") != "approval"

    # /answer should ALSO advance to the Final and return a terminal RunResult.
    r2 = client.post(
        f"/sessions/{sid}/answer",
        json={"material_id": pending_id, "answer_text": "Maria's Pizzeria"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "completed"
    assert body["terminal"] is True


# ---------------------------------------------------------------------------
# 5. 404 on missing session
# ---------------------------------------------------------------------------


def test_404_on_missing_session(make_client):
    client = make_client([])
    fake = "00000000-0000-7000-8000-000000000000"

    r = client.get(f"/sessions/{fake}")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "not_found"
    assert isinstance(body["detail"], dict)
    assert body["detail"]["session_id"] == fake

    r = client.post(f"/sessions/{fake}/resume", json={})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"

    r = client.post(
        f"/sessions/{fake}/answer",
        json={"material_id": "irrelevant", "answer_text": "x"},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


# ---------------------------------------------------------------------------
# 6. Spend summary shape after MockWorker turns
# ---------------------------------------------------------------------------


def test_spend_summary_empty_after_mock_run(make_client):
    """MockWorker bypasses the LLM client, so no spend rows are written.

    The shape of an empty spend_summary must still match the doc exactly.
    """
    client = make_client(_restaurant_short_script())
    sid = client.post("/sessions", json={}).json()["session_id"]

    client.post(f"/sessions/{sid}/resume", json={})
    detail = client.get(f"/sessions/{sid}").json()
    pending_id = detail["pending_materials"][0]["id"]
    client.post(
        f"/sessions/{sid}/answer",
        json={"material_id": pending_id, "approved": True},
    )

    final = client.get(f"/sessions/{sid}").json()
    assert final["spend_summary"] == {
        "total_usd": 0.0,
        "by_model": {},
        "fallback_count": 0,
    }


# ---------------------------------------------------------------------------
# 7. No auto-seed on session creation
# ---------------------------------------------------------------------------


def test_create_session_does_not_auto_seed_brief(make_client, tmp_path: Path):
    """POST /sessions must NOT pre-populate a business_brief material.

    The bootstrap flow (chat worker collects info via ask_user; user approves
    via request_approval(subject='business_brief')) is the only path that
    persists the brief. Auto-seeding here previously caused render_mockup to
    theme every real user's site with the placeholder "Maria's Pizzeria".
    """
    client = make_client([])
    sid = client.post("/sessions", json={}).json()["session_id"]

    # Open the per-session DB directly (auth-free: it's our test sandbox).
    # The TestClient's ctx writes to tmp_path/data/sessions/<sid>.db. If the
    # file doesn't exist at all that's a stronger pass — it means POST
    # /sessions did not touch the per-session DB. If it exists, the
    # business_brief table slice must be empty.
    db_path = tmp_path / "data" / "sessions" / f"{sid}.db"
    if db_path.is_file():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT type, content, pending FROM material"
        ).fetchall()
        conn.close()
        briefs = [r for r in rows if r["type"] == "business_brief"]
        assert briefs == [], (
            f"expected no business_brief materials immediately after"
            f" POST /sessions, got {len(briefs)}"
        )


# ---------------------------------------------------------------------------
# Bonus: malformed body returns the 400 error shape
# ---------------------------------------------------------------------------


def test_400_on_answer_without_material_id(make_client):
    client = make_client([])
    sid = client.post("/sessions", json={}).json()["session_id"]

    r = client.post(f"/sessions/{sid}/answer", json={})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "bad_request"
    assert isinstance(body["detail"], dict)


def test_answer_continuation_approval_resets_iter_counter(
    make_client, tmp_path: Path
):
    """Full HTTP path: trip the iter cap, approve via /answer, verify reset.

    Drives MockWorker for two ToolCall turns -> Final. We pre-set
    iter_since_approval to the cap so the first /resume call lands directly
    on the iter-cap branch (which now persists a pending continuation
    material). POST /answer with approved=True flows through the existing
    approval handler; the orchestrator's Issue-1 fix resets the counter on
    the next turn's human_resumed processing.
    """
    scripted: list[ToolCall | Final | Escalate] = [
        Final(summary="resumed after cap"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    # Reach into the core DB to force iter_since_approval = cap.
    core_db = tmp_path / "data" / "harness.db"
    assert core_db.is_file()
    conn = store.core_connection(core_db)
    try:
        store.update_session_status(
            conn, sid, "active", iter_since_approval=10
        )
    finally:
        conn.close()

    # First resume hits the iter cap. The orchestrator persists a
    # continuation_approval pending material.
    r1 = client.post(f"/sessions/{sid}/resume", json={})
    assert r1.status_code == 200
    assert r1.json()["status"] == "awaiting_human"
    assert r1.json()["paused_reason"] == "turn_cap"

    detail = client.get(f"/sessions/{sid}").json()
    pending = detail["pending_materials"]
    cont = [
        m for m in pending if m["content"].get("kind") == "continuation_approval"
    ]
    assert len(cont) == 1, f"expected 1 continuation_approval, got {pending}"
    pending_id = cont[0]["id"]

    # Approve the continuation via /answer. The endpoint's `is_approval`
    # check was widened to accept continuation_approval, so this body shape
    # (with `approved` and no `answer_text`) is valid.
    r2 = client.post(
        f"/sessions/{sid}/answer",
        json={"material_id": pending_id, "approved": True},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    # The Final fires on the next worker turn after the approval is folded
    # in, so the resulting RunResult is terminal.
    assert body["status"] == "completed"

    # Inspect the final session row directly: iter_since_approval was reset.
    conn = store.core_connection(core_db)
    try:
        session_row = store.load_session(conn, sid)
    finally:
        conn.close()
    assert session_row is not None
    assert int(session_row["iter_since_approval"]) == 0


# ---------------------------------------------------------------------------
# /resume unstick: force_continue auto-approves continuation_approval
# ---------------------------------------------------------------------------


def test_resume_unsticks_session_in_iter_cap(make_client, tmp_path: Path):
    """Full HTTP path: trip the iter cap, then POST /resume (NOT /answer).

    The /resume handler must:
      - call force_continue, which auto-approves the pending
        continuation_approval and resets the iter counter
      - run the loop forward; the scripted MockWorker returns Final on the
        next turn so the session terminates cleanly
      - resolve the iteration_limit_reached alarm (state-based, condition no
        longer holds because counter is now 0)
      - mark the original continuation_approval pending row resolved
    """
    scripted: list[ToolCall | Final | Escalate] = [
        Final(summary="unstuck via /resume"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    # Force iter_since_approval to the cap so the first /resume trips it.
    core_db = tmp_path / "data" / "harness.db"
    conn = store.core_connection(core_db)
    try:
        store.update_session_status(
            conn, sid, "active", iter_since_approval=10
        )
    finally:
        conn.close()

    # First /resume: trips iter cap, persists a continuation_approval, pauses.
    r1 = client.post(f"/sessions/{sid}/resume", json={})
    assert r1.status_code == 200
    assert r1.json()["status"] == "awaiting_human"
    assert r1.json()["paused_reason"] == "turn_cap"

    detail = client.get(f"/sessions/{sid}").json()
    pending = [
        m for m in detail["pending_materials"]
        if m["content"].get("kind") == "continuation_approval"
    ]
    assert len(pending) == 1
    cont_mid = pending[0]["id"]

    # Second /resume: must auto-approve the continuation (no /answer call) and
    # drive the loop to the Final on the next turn.
    r2 = client.post(f"/sessions/{sid}/resume", json={})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "completed"
    assert body["terminal"] is True

    final = client.get(f"/sessions/{sid}").json()
    # The continuation_approval pending row is now resolved.
    cont_after = [
        m for m in final["pending_materials"]
        if m["id"] == cont_mid
    ]
    assert cont_after == []
    # The iteration_limit_reached alarm auto-resolves once the counter is 0.
    iter_alarms = [
        a for a in final["alarms"] if a["type"] == "iteration_limit_reached"
    ]
    assert iter_alarms, "expected an iteration_limit_reached alarm to have fired"
    assert all(a["resolved"] is True for a in iter_alarms)


def test_resume_on_completed_session_is_noop(make_client, tmp_path: Path):
    """POST /resume on a completed session must be a no-op.

    Status stays 'completed', no events from force_continue (there are no
    pending continuation_approvals to auto-approve, and the status check
    leaves the completed session alone).
    """
    # Drive the session to completion first.
    scripted: list[ToolCall | Final | Escalate] = [Final(summary="done.")]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.json()["status"] == "completed"

    # Snapshot events + materials before the no-op /resume.
    sessions_dir = tmp_path / "data" / "sessions"
    db_path = sessions_dir / f"{sid}.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    events_before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    materials_before = conn.execute(
        "SELECT COUNT(*) AS n FROM material"
    ).fetchone()["n"]
    conn.close()

    # Second /resume — should be a no-op since the session is completed and
    # has no pending continuation_approval to auto-approve.
    r2 = client.post(f"/sessions/{sid}/resume", json={})
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    events_after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    materials_after = conn.execute(
        "SELECT COUNT(*) AS n FROM material"
    ).fetchone()["n"]
    conn.close()
    assert events_after == events_before
    assert materials_after == materials_before


def test_resume_on_awaiting_human_with_ask_user_pending_does_not_auto_answer(
    make_client,
):
    """POST /resume on an ask_user-paused session must NOT auto-answer.

    The pending ask_user material stays pending, but force_continue still
    flips the session status from awaiting_human back to active. The
    orchestrator then runs the next turn; the MockWorker is scripted to
    return Final so the session terminates cleanly and we can make crisp
    assertions about the end state.

    Documented behavior: the ask_user pending material remains pending
    AFTER /resume so the operator still has the opportunity to /answer it
    explicitly later if needed. Force_continue is purely a safety-gate
    override, not an answer-skipper.
    """
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="ask_user", args={"question": "what is your name?"}),
        Final(summary="agent finished anyway"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    # First /resume hits the ask_user pause.
    r1 = client.post(f"/sessions/{sid}/resume", json={})
    assert r1.json()["status"] == "awaiting_human"
    assert r1.json()["paused_reason"] == "awaiting_human"

    detail = client.get(f"/sessions/{sid}").json()
    ask_pending = [
        m for m in detail["pending_materials"]
        if m["content"].get("kind") != "approval"
        and m["content"].get("kind") != "continuation_approval"
    ]
    assert len(ask_pending) == 1
    ask_mid = ask_pending[0]["id"]

    # Second /resume — force_continue should NOT touch the ask_user pending.
    # It flips status to active; the loop then runs the next scripted Final.
    r2 = client.post(f"/sessions/{sid}/resume", json={})
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "completed"
    assert body["terminal"] is True

    final = client.get(f"/sessions/{sid}").json()
    # The ask_user material is STILL pending (force_continue did not answer it).
    survived = [m for m in final["pending_materials"] if m["id"] == ask_mid]
    assert len(survived) == 1
    assert survived[0]["pending"] is True


def test_400_on_answer_to_approval_without_approved(make_client):
    """Pending approval requires `approved` in the body; otherwise 400."""
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(
            tool="request_approval",
            args={"subject": "mockup", "details": None},
        ),
        Final(summary="unused"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]
    client.post(f"/sessions/{sid}/resume", json={})

    detail = client.get(f"/sessions/{sid}").json()
    pending_id = detail["pending_materials"][0]["id"]

    # Send only answer_text — the pending material is an approval, so this
    # is a shape mismatch.
    r = client.post(
        f"/sessions/{sid}/answer",
        json={"material_id": pending_id, "answer_text": "yes"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


# ---------------------------------------------------------------------------
# /rewind — truncate the session to a previous awaiting_human event
# ---------------------------------------------------------------------------


def test_rewind_route_truncates_session(make_client):
    """Drive past two ask_user pauses; rewind to the first; assert truncation.

    The session ends up back at the first pause: events count drops to ~3
    (worker_input, worker_output, awaiting_human), the first pending
    material is re-pended, and a `rewound` audit event appears at the tail.
    """
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="ask_user", args={"question": "first?"}),
        ToolCall(tool="ask_user", args={"question": "second?"}),
        Final(summary="done"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    # Drive to the first awaiting_human pause.
    client.post(f"/sessions/{sid}/resume", json={})
    detail = client.get(f"/sessions/{sid}").json()
    first_awaiting = next(
        e for e in detail["events"] if e["type"] == "awaiting_human"
    )
    pending_id = detail["pending_materials"][0]["id"]

    # Answer the first ask_user and drive to the second pause.
    client.post(
        f"/sessions/{sid}/answer",
        json={"material_id": pending_id, "answer_text": "Alice"},
    )
    detail_mid = client.get(f"/sessions/{sid}").json()
    assert detail_mid["session"]["status"] == "awaiting_human"
    assert len(detail_mid["pending_materials"]) >= 1

    # POST /rewind with the first awaiting_human's id.
    r = client.post(
        f"/sessions/{sid}/rewind",
        json={"target_event_id": first_awaiting["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == sid
    assert body["target_event_id"] == first_awaiting["id"]
    assert body["repended_material_id"] == pending_id
    assert isinstance(body["rewind_event_id"], str)
    assert body["removed_events"] > 0

    # Re-fetch detail: ~3 events (worker_input, worker_output, awaiting_human)
    # plus the trailing `rewound` audit event = ~4. Pending material is back.
    final = client.get(f"/sessions/{sid}").json()
    types_now = [e["type"] for e in final["events"]]
    assert types_now[-1] == "rewound"
    # The first ask_user material is pending again.
    assert any(m["id"] == pending_id for m in final["pending_materials"])
    # Status is awaiting_human.
    assert final["session"]["status"] == "awaiting_human"


def test_rewind_route_400_on_bad_target(make_client):
    """POST /rewind with a non-awaiting_human event id -> 400."""
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="ask_user", args={"question": "q?"}),
        Final(summary="done"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]
    client.post(f"/sessions/{sid}/resume", json={})

    detail = client.get(f"/sessions/{sid}").json()
    worker_output = next(
        e for e in detail["events"] if e["type"] == "worker_output"
    )

    r = client.post(
        f"/sessions/{sid}/rewind",
        json={"target_event_id": worker_output["id"]},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "bad_request"
    assert "target_event_id" in body["detail"]


def test_rewind_route_404_on_missing_session(make_client):
    client = make_client([])
    fake = "00000000-0000-7000-8000-000000000000"
    r = client.post(
        f"/sessions/{fake}/rewind",
        json={"target_event_id": "anything"},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_session_detail_includes_llm_calls_list(make_client, tmp_path: Path):
    """GET /sessions/{id} JSON response includes an ``llm_calls`` array.

    MockWorker bypasses the LLM client so we pre-seed an llm_calls row via
    ``store.record_llm_call`` directly. The endpoint must surface it in the
    JSON response with parsed (not nested-stringified) request_messages.
    """
    client = make_client([])
    sid = client.post("/sessions", json={}).json()["session_id"]

    sessions_dir = tmp_path / "data" / "sessions"
    conn = store.session_connection(sessions_dir, sid)
    try:
        store.record_llm_call(
            conn,
            model="deepseek/deepseek-v4-flash:free",
            is_fallback=False,
            request_messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
            request_options={
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            response_text='{"type":"final","summary":"ok"}',
            finish_reason="stop",
            tokens_in=5,
            tokens_out=7,
            cost_usd=0.0012,
            status="ok",
        )
    finally:
        conn.close()

    body = client.get(f"/sessions/{sid}").json()
    assert "llm_calls" in body
    assert isinstance(body["llm_calls"], list)
    assert len(body["llm_calls"]) == 1
    call = body["llm_calls"][0]
    assert call["model"] == "deepseek/deepseek-v4-flash:free"
    assert call["status"] == "ok"
    assert call["tokens_in"] == 5
    assert call["tokens_out"] == 7
    assert call["cost_usd"] == pytest.approx(0.0012)
    assert call["response_text"] == '{"type":"final","summary":"ok"}'
    # request_messages must come back as a parsed list of dicts, not a
    # JSON-encoded string.
    assert isinstance(call["request_messages"], list)
    assert call["request_messages"][0]["role"] == "system"
    assert isinstance(call["request_options"], dict)
    assert call["request_options"]["temperature"] == 0.2


def test_rewind_route_400_on_missing_body(make_client):
    """POST /rewind with no body or no target_event_id -> 400."""
    client = make_client([])
    sid = client.post("/sessions", json={}).json()["session_id"]

    # Missing target_event_id field.
    r = client.post(f"/sessions/{sid}/rewind", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"

    # Empty string target_event_id (min_length=1).
    r2 = client.post(
        f"/sessions/{sid}/rewind", json={"target_event_id": ""}
    )
    assert r2.status_code == 400
