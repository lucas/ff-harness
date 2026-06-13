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
    }

    # Session row shape.
    assert body["session"]["id"] == sid
    assert body["session"]["status"] == "active"
    assert body["session"]["current_stage"] == "bootstrap"

    # Fresh session — no events yet (brief was seeded without an event).
    assert body["events"] == []
    assert body["checkpoints"] == []
    assert body["alarms"] == []
    # Brief was seeded with pending=False — should not appear in pending list.
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
# 7. Auto-seed restaurant brief
# ---------------------------------------------------------------------------


def test_create_session_auto_seeds_restaurant_brief(make_client, tmp_path: Path):
    """POST /sessions must seed a business_brief material with the restaurant data."""
    client = make_client([])
    sid = client.post("/sessions", json={}).json()["session_id"]

    # Open the per-session DB directly (auth-free: it's our test sandbox).
    # The TestClient's ctx writes to tmp_path/data/sessions/<sid>.db.
    db_path = tmp_path / "data" / "sessions" / f"{sid}.db"
    assert db_path.is_file()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT type, content, pending FROM material"
    ).fetchall()
    conn.close()

    briefs = [r for r in rows if r["type"] == "business_brief"]
    assert len(briefs) == 1, f"expected 1 brief, got {len(briefs)}"
    # Seeded with pending=False (we don't want it appearing in /pending).
    assert briefs[0]["pending"] == 0
    import json as _json
    content = _json.loads(briefs[0]["content"])
    assert content["name"] == "Maria's Pizzeria"
    assert content["industry"] == "restaurant"


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


def test_message_endpoint_appends_user_input_and_resumes(
    make_client, tmp_path: Path
):
    """POST /message persists a user_answer w/ unprompted=True, appends
    human_resumed, flips status to active, and drives the loop to completion."""
    scripted: list[ToolCall | Final | Escalate] = [
        Final(summary="acknowledged"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    r = client.post(
        f"/sessions/{sid}/message",
        json={"content": "build a one-page site for a coffee shop"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["terminal"] is True

    detail = client.get(f"/sessions/{sid}").json()
    assert detail["session"]["status"] == "completed"
    # Event log includes the human_resumed with our unprompted text.
    resumed = [e for e in detail["events"] if e["type"] == "human_resumed"]
    assert len(resumed) >= 1
    found = False
    for ev in resumed:
        a = (ev.get("payload") or {}).get("answer_or_decision") or {}
        if (
            a.get("answer_text") == "build a one-page site for a coffee shop"
            and a.get("unprompted") is True
        ):
            found = True
            break
    assert found, f"unprompted human_resumed not found in events: {resumed!r}"

    # Material row exists with unprompted=True in content.
    db_path = tmp_path / "data" / "sessions" / f"{sid}.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT type, content FROM material WHERE type = 'user_answer'"
    ).fetchall()
    conn.close()
    import json as _json
    matched = [
        r for r in rows if _json.loads(r["content"]).get("unprompted") is True
    ]
    assert len(matched) == 1
    assert (
        _json.loads(matched[0]["content"])["answer_text"]
        == "build a one-page site for a coffee shop"
    )


def test_message_endpoint_400_on_empty_content(make_client):
    """Both empty string and missing field return the standard 400 shape."""
    client = make_client([])
    sid = client.post("/sessions", json={}).json()["session_id"]

    r = client.post(f"/sessions/{sid}/message", json={"content": ""})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"

    r = client.post(f"/sessions/{sid}/message", json={"content": "   "})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"

    r = client.post(f"/sessions/{sid}/message", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_message_endpoint_404_on_missing_session(make_client):
    client = make_client([])
    fake = "00000000-0000-7000-8000-000000000000"
    r = client.post(f"/sessions/{fake}/message", json={"content": "hello"})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_message_endpoint_reactivates_completed_session(make_client):
    """A completed session must accept a /message and resume; status reflects
    the new MockWorker response (here: another Final -> completed)."""
    scripted: list[ToolCall | Final | Escalate] = [
        Final(summary="first done"),
        Final(summary="second done"),
    ]
    client = make_client(scripted)
    sid = client.post("/sessions", json={}).json()["session_id"]

    r1 = client.post(f"/sessions/{sid}/resume", json={})
    assert r1.json()["status"] == "completed"

    # Drive it again via /message; the second Final fires.
    r2 = client.post(
        f"/sessions/{sid}/message",
        json={"content": "actually, please reconsider"},
    )
    assert r2.status_code == 200
    body = r2.json()
    # The MockWorker is called a SECOND time and returns a fresh Final.
    assert body["status"] == "completed"
    assert body["terminal"] is True


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
