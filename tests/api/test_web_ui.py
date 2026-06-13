"""Step 10 gate — HTML routes + templates rendered by FastAPI TestClient.

Strategy mirrors `tests/api/test_web_api.py`:
  - Each test gets its own `tmp_path`-rooted AppContext.
  - `AppContext.worker_for_stage_factory` is overridden so every stage hands
    back the same scripted MockWorker. No real LLM / network calls.
  - The TestClient drives only HTTP — no uvicorn process is ever started.

The goal is to prove the server-rendered web UI works end-to-end:
  - GET /                             -> index.html
  - GET /sessions/{id}/view           -> session.html (full page)
  - GET /sessions/{id}/view?partial=1 -> _session_main.html (fragment)
  - GET /sites/{id}/index.html        -> StaticFiles mount serves the file
  - 404 / empty-state behaviour
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.api.app import create_app
from harness.api.dependencies import AppContext
from harness.models.envelope import Escalate, Final, ToolCall
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
    """Build an AppContext with the MockWorker handed to every stage."""
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
        # OpenRouterClient is never called in MockWorker mode.
        llm_client=OpenRouterClient(api_key="test-key-not-used"),
        worker_for_stage_factory=factory,
    )


@pytest.fixture
def make_test_app(
    tmp_path: Path,
) -> Iterator[Callable[..., tuple[TestClient, Path]]]:
    """Return a factory that builds (TestClient, tmp_data_dir) pairs.

    `mock_responses` defaults to an empty script; pass a list of
    `ToolCall | Final | Escalate` to drive a flow.
    """

    def _make(
        mock_responses: list[ToolCall | Final | Escalate] | None = None,
    ) -> tuple[TestClient, Path]:
        worker = MockWorker(scripted_responses=mock_responses or [])
        data_dir = tmp_path / "data"
        ctx = _make_test_app_context(data_dir=data_dir, mock_worker=worker)
        app = create_app(ctx)
        return TestClient(app), data_dir

    yield _make


def _create_session(client: TestClient) -> str:
    """POST /sessions, return the session_id."""
    r = client.post("/sessions", json={})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


# ---------------------------------------------------------------------------
# 1. Home page renders
# ---------------------------------------------------------------------------


def test_home_renders(make_test_app):
    client, _ = make_test_app()
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"].lower()
    body = r.text
    assert re.search(r"sessions", body, re.IGNORECASE), body[:500]
    # The base-page <title> is "Sessions — Harness" (from index.html).
    assert "<title>" in body.lower()
    assert "Harness" in body


# ---------------------------------------------------------------------------
# 2. Session detail page renders
# ---------------------------------------------------------------------------


def test_session_view_renders(make_test_app):
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"].lower()
    body = r.text

    # First 8 chars of the session id appear in the header.
    assert sid[:8] in body, "session id (short) not found in page"
    # Stage and status text rendered.
    assert "bootstrap" in body  # current_stage
    assert "active" in body  # status


# ---------------------------------------------------------------------------
# 3. Partial fragment (no <!doctype html>)
# ---------------------------------------------------------------------------


def test_session_view_partial(make_test_app):
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view", params={"partial": "1"})
    assert r.status_code == 200
    body = r.text
    # Fragment only: no full document declaration.
    assert "<!doctype html" not in body.lower()
    assert "<html" not in body.lower()
    # But it must contain one of the section headers from _session_main.html.
    # Any of Events / Checkpoints / Alarms / Cost should be present.
    assert any(
        marker in body
        for marker in ("Events", "Checkpoints", "Alarms", "Cost")
    ), body[:500]


# ---------------------------------------------------------------------------
# 4. Pending-question form renders
# ---------------------------------------------------------------------------


def _approval_then_final() -> list[ToolCall | Final | Escalate]:
    """Scripted: request_approval (pauses on awaiting_human) -> Final (unused)."""
    return [
        ToolCall(
            tool="request_approval",
            args={"subject": "mockup", "details": {"note": "ready"}},
        ),
        Final(summary="never reached in this test"),
    ]


def test_pending_form_renders(make_test_app):
    client, _ = make_test_app(_approval_then_final())
    sid = _create_session(client)

    # Drive one turn: hits request_approval -> session pauses with pending material.
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    # The detail page should now render the awaiting form.
    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    # The page submits to /sessions/{id}/answer. The route is constructed in
    # JS as `'/sessions/' + sessionId + '/answer'`, so the full literal URL
    # doesn't appear in the HTML — instead, assert on the pieces:
    #   - the session id is embedded (via tojson in the script block)
    #   - the '/answer' suffix appears in the fetch URL construction
    # Also tolerate the simpler case where a future template uses a literal
    # form action="/sessions/{id}/answer".
    answer_route_literal = f"/sessions/{sid}/answer"
    js_fragment = "'/sessions/' + sessionId + '/answer'"
    assert (
        answer_route_literal in body
        or (sid in body and js_fragment in body)
    ), (
        "expected the answer endpoint to be referenced in the page; "
        "looked for a literal action attribute or the JS fetch construction"
    )

    # At least one approve/deny control should be present.
    # The awaiting.html template uses two submit buttons named "approved"
    # with values "true" / "false".
    has_approve_button = (
        'name="approved" value="true"' in body
        or 'name="approved" value="false"' in body
    )
    # Fallback: just look for the visible button labels.
    has_button_labels = "Approve" in body and "Deny" in body
    assert has_approve_button or has_button_labels, (
        "expected approve/deny form controls in the rendered page"
    )


# ---------------------------------------------------------------------------
# 5. 404 on missing session
# ---------------------------------------------------------------------------


def test_404_on_missing_session(make_test_app):
    client, _ = make_test_app()
    fake = "00000000-0000-7000-0000-000000000000"
    r = client.get(f"/sessions/{fake}/view")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 6. Static-site mount serves files written to tmp_data_dir/sites/{id}/
# ---------------------------------------------------------------------------


def test_static_site_serving(make_test_app):
    client, data_dir = make_test_app()
    sid = _create_session(client)

    # Write a tiny HTML file under the per-session sites dir.
    site_dir = data_dir / "sites" / sid
    site_dir.mkdir(parents=True, exist_ok=True)
    payload = "<html><body><h1>hello from static mount</h1></body></html>"
    (site_dir / "index.html").write_text(payload, encoding="utf-8")

    r = client.get(f"/sites/{sid}/index.html")
    assert r.status_code == 200, r.text
    assert r.text == payload


# ---------------------------------------------------------------------------
# 7. Empty-state templates render without exceptions
# ---------------------------------------------------------------------------


def test_polling_removed(make_test_app):
    """Regression guard: the session detail page must not include a JS
    polling loop. setInterval would clobber form state / details toggles."""
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    assert "setinterval" not in r.text.lower(), (
        "session.html must not include setInterval polling"
    )


def test_templates_handle_empty_state(make_test_app):
    """A freshly-created session has no events / checkpoints / alarms /
    pending materials / spend rows. The detail page must still render."""
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text
    # Sanity: at least one of the empty-state strings from the templates
    # (verifies the empty branches were taken without error).
    assert any(
        empty_marker in body
        for empty_marker in (
            "No events yet.",
            "No checkpoints yet.",
            "No alarms.",
            "Nothing pending.",
            "No spend recorded.",
        )
    ), body[:800]
