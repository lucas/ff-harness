"""Step 10 gate (chat-first redesign) — HTML routes + templates.

Strategy mirrors `tests/api/test_web_api.py`:
  - Each test gets its own `tmp_path`-rooted AppContext.
  - `AppContext.worker_for_stage_factory` is overridden so every stage hands
    back the same scripted MockWorker. No real LLM / network calls.
  - The TestClient drives only HTTP — no uvicorn process is ever started.

Validated:
  - GET /                             -> index.html
  - GET /sessions/{id}/view           -> session.html (full chat-first page)
  - GET /sessions/{id}/view?partial=1 -> _session_main.html (fragment)
  - GET /sites/{id}/index.html        -> StaticFiles mount serves the file
  - chat panel: agent/user bubbles, per-tool summaries (no raw JSON)
  - input area: context-sensitive on session status + pending materials
  - details accordion: collapsed by default; events use HH:MM:SS and per-type summaries
  - continuation_approval: renders Approve/Stop buttons
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
        llm_client=OpenRouterClient(api_key="test-key-not-used"),
        worker_for_stage_factory=factory,
    )


@pytest.fixture
def make_test_app(
    tmp_path: Path,
) -> Iterator[Callable[..., tuple[TestClient, Path]]]:
    """Return a factory that builds (TestClient, tmp_data_dir) pairs."""

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


def _approval_then_final() -> list[ToolCall | Final | Escalate]:
    return [
        ToolCall(
            tool="request_approval",
            args={"subject": "mockup", "details": {"note": "ready"}},
        ),
        Final(summary="never reached in this test"),
    ]


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
    assert "<title>" in body.lower()
    assert "Harness" in body


# ---------------------------------------------------------------------------
# 2. Session detail page renders the chat-first layout
# ---------------------------------------------------------------------------


def test_session_view_renders(make_test_app):
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    assert sid[:8] in body, "session id (short) not found in page"
    assert "bootstrap" in body
    assert "active" in body
    # The chat panel and details accordion both render on a fresh session.
    assert "chat-panel" in body
    assert "<details" in body


# ---------------------------------------------------------------------------
# 3. Partial fragment (no <!doctype html>)
# ---------------------------------------------------------------------------


def test_session_view_partial(make_test_app):
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view", params={"partial": "1"})
    assert r.status_code == 200
    body = r.text
    assert "<!doctype html" not in body.lower()
    assert "<html" not in body.lower()
    # The fragment contains the chat panel + the section headers.
    assert "chat-panel" in body
    assert any(
        marker in body
        for marker in ("Events", "Checkpoints", "Alarms", "Cost")
    ), body[:500]


# ---------------------------------------------------------------------------
# 4. Chat panel renders an agent bubble + a user input affordance — no
#    raw JSON in the chat area.
# ---------------------------------------------------------------------------


def test_chat_panel_renders(make_test_app):
    """After a request_approval turn, the page must show:
      - an agent bubble in the chat log (CSS class .chat-msg.agent)
      - an input affordance (form) for the pending material
      - NO raw JSON dump like `{"args":` in the chat area
    """
    client, _ = make_test_app(_approval_then_final())
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    # An agent bubble must be present.
    assert "chat-msg agent" in body
    # The user input form must be present.
    assert "answer-form" in body
    # The bubble must use the friendly summary, not the raw envelope JSON.
    # The orchestrator's worker_output payload contains `"envelope":` — that
    # must NOT bleed into the chat log. We allow it inside the events table
    # only because the events table also doesn't render it now; assert it
    # never appears in this view.
    chat_section_start = body.find('id="chat-log"')
    assert chat_section_start != -1
    chat_section_end = body.find('id="chat-input"')
    chat_section = body[chat_section_start:chat_section_end]
    assert '"args":' not in chat_section
    assert '"envelope":' not in chat_section


# ---------------------------------------------------------------------------
# 5. Pending-question form: continuation_approval renders Approve + Stop
# ---------------------------------------------------------------------------


def test_continuation_approval_renders_approve_stop_buttons(make_test_app):
    """Drive past the turn cap (10 turns by default), then GET /view —
    the input area should render Approve + Stop buttons because the
    orchestrator persisted a continuation_approval pending material.
    """
    # Use a worker that always returns Final-but-not-yet — actually we need
    # the worker to keep producing tool_calls that don't pause until the cap
    # trips. Simpler: feed 11 list_files calls (a tool with no pause), the
    # 11th turn will trip the cap before being called.
    cap_responses: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="list_files", args={"path": "."}) for _ in range(11)
    ]
    client, _ = make_test_app(cap_responses)
    sid = _create_session(client)

    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    # Status should be awaiting_human, paused on the turn-cap.
    body_json = r.json()
    assert body_json["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text
    # The continuation_approval form must show Approve + Stop buttons.
    # Match case-insensitive on the rendered button labels.
    lower = body.lower()
    assert ">approve<" in lower or "approve" in lower
    assert ">stop<" in lower or "stop" in lower
    # The form data-is-continuation flag must be set on this material's form.
    assert 'data-is-continuation="1"' in body


# ---------------------------------------------------------------------------
# 6. Pending-question form for request_approval renders Approve/Deny
# ---------------------------------------------------------------------------


def test_pending_form_renders(make_test_app):
    client, _ = make_test_app(_approval_then_final())
    sid = _create_session(client)

    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    # The page JS posts to /sessions/{id}/answer.
    answer_route_literal = f"/sessions/{sid}/answer"
    js_fragment = "'/sessions/' + sessionId + '/answer'"
    assert (
        answer_route_literal in body
        or (sid in body and js_fragment in body)
    )

    # Approve + Deny buttons present (request_approval, not continuation).
    has_approve_button = (
        'name="approved" value="true"' in body
        or 'name="approved" value="false"' in body
    )
    has_button_labels = "Approve" in body and "Deny" in body
    assert has_approve_button or has_button_labels


# ---------------------------------------------------------------------------
# 7. Events table uses human summaries (HH:MM:SS time, no JSON expanders)
# ---------------------------------------------------------------------------


def test_events_table_has_human_summaries(make_test_app):
    """Drive a few turns; the events table must use per-type summaries
    (no <pre> JSON dumps in the events region) and at least one
    HH:MM:SS-shaped time string must be present."""
    client, _ = make_test_app(_approval_then_final())
    sid = _create_session(client)
    client.post(f"/sessions/{sid}/resume", json={})

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    events_start = body.find('id="events-card"')
    assert events_start != -1, "events card not found"
    # Slice from events-card forward to a stable downstream anchor.
    cost_start = body.find('id="cost-card"', events_start)
    events_section = body[events_start:cost_start]

    # No <pre> dumps of payload JSON in the events section.
    assert "<pre>" not in events_section, events_section[:1000]
    # An HH:MM:SS time string must appear in the events area.
    assert re.search(r"\b\d{2}:\d{2}:\d{2}\b", events_section), (
        "expected at least one HH:MM:SS timestamp in the events section"
    )


# ---------------------------------------------------------------------------
# 8. Details accordion is collapsed by default
# ---------------------------------------------------------------------------


def test_details_accordion_collapsed_by_default(make_test_app):
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    # Find the details wrapping the accordion. It must NOT have `open` set.
    m = re.search(r'<details\s+id="details-section"([^>]*)>', body)
    assert m is not None, "details-section accordion not found"
    attrs = m.group(1)
    assert "open" not in attrs, f"details section should be collapsed by default, attrs={attrs!r}"


# ---------------------------------------------------------------------------
# 9. Completed sessions disable the input textarea
# ---------------------------------------------------------------------------


def test_input_disabled_when_completed(make_test_app):
    """Drive a single Final envelope to completion; the input area must
    render a disabled textarea (no Resume button)."""
    client, _ = make_test_app([Final(summary="done")])
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    r = client.get(f"/sessions/{sid}/view")
    body = r.text

    # Extract the input area and assert the textarea is disabled.
    input_start = body.find('id="chat-input"')
    assert input_start != -1
    # Grab the next ~2000 chars; the input area is bounded but we don't need
    # to be exact — just confirm a disabled textarea is in there.
    input_section = body[input_start : input_start + 2000]
    assert "<textarea" in input_section
    assert "disabled" in input_section, input_section


# ---------------------------------------------------------------------------
# 10. No JS polling loop in session.html
# ---------------------------------------------------------------------------


def test_polling_removed(make_test_app):
    """Regression guard: setInterval polling would clobber form state."""
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    assert "setinterval" not in r.text.lower()


# ---------------------------------------------------------------------------
# 11. 404 on missing session
# ---------------------------------------------------------------------------


def test_404_on_missing_session(make_test_app):
    client, _ = make_test_app()
    fake = "00000000-0000-7000-0000-000000000000"
    r = client.get(f"/sessions/{fake}/view")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 12. Static-site mount serves files written to tmp_data_dir/sites/{id}/
# ---------------------------------------------------------------------------


def test_static_site_serving(make_test_app):
    client, data_dir = make_test_app()
    sid = _create_session(client)

    site_dir = data_dir / "sites" / sid
    site_dir.mkdir(parents=True, exist_ok=True)
    payload = "<html><body><h1>hello from static mount</h1></body></html>"
    (site_dir / "index.html").write_text(payload, encoding="utf-8")

    r = client.get(f"/sites/{sid}/index.html")
    assert r.status_code == 200, r.text
    assert r.text == payload


# ---------------------------------------------------------------------------
# 13. Empty-state templates render without exceptions
# ---------------------------------------------------------------------------


def test_templates_handle_empty_state(make_test_app):
    """A freshly-created session has no events / checkpoints / alarms /
    pending materials / spend rows. The detail page must still render."""
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text
    assert any(
        empty_marker in body
        for empty_marker in (
            "No events yet.",
            "No checkpoints yet.",
            "No alarms.",
            "No conversation yet.",
            "No spend recorded.",
        )
    ), body[:800]


# ---------------------------------------------------------------------------
# 14. Agent bubble markdown renders in the chat panel
# ---------------------------------------------------------------------------


def test_agent_bubble_renders_markdown_in_chat_panel(make_test_app):
    """A Final envelope with markdown body must produce a chat bubble whose
    rendered HTML contains <strong>...</strong>, not the literal `**...**`.
    """
    client, _ = make_test_app(
        [Final(summary="**Done!** Site shipped.\n\n- one\n- two")]
    )
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    r = client.get(f"/sessions/{sid}/view")
    body = r.text
    chat_start = body.find('id="chat-log"')
    chat_end = body.find('id="chat-input"', chat_start)
    chat_section = body[chat_start:chat_end]

    assert "<strong>Done!</strong>" in chat_section, chat_section[:1200]
    assert "<li>one</li>" in chat_section
    assert "<li>two</li>" in chat_section
    # The literal markdown source must not also appear inside the bubble.
    assert "**Done!**" not in chat_section


# ---------------------------------------------------------------------------
# 15. Input area does not restate the approval subject
# ---------------------------------------------------------------------------


def _slice_input_area(body: str) -> str:
    """Return the substring of ``body`` from id="chat-input" to the
    closing </section> of the chat panel — the input-area subtree.
    """
    start = body.find('id="chat-input"')
    assert start != -1, "chat-input not found"
    end = body.find("</section>", start)
    return body[start:end]


def test_input_area_does_not_restate_approval_subject(make_test_app):
    """For a request_approval pending material the bubble above already
    shows the subject. The input area must render only the action controls.
    """
    client, _ = make_test_app(_approval_then_final())
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    body = r.text
    input_section = _slice_input_area(body)

    # The subject "mockup" appears in the agent bubble; the input area must
    # NOT repeat it.
    assert "mockup" not in input_section, input_section[:2000]
    # And the old restatement label must be gone too.
    assert "Approval requested" not in input_section
    # The collapsible "Details" accordion inside the input area is gone.
    assert "Details</summary>" not in input_section
    # Action buttons are still present.
    assert "Approve" in input_section
    assert "Deny" in input_section


def test_input_area_does_not_restate_continuation_question(make_test_app):
    """The continuation_approval bubble carries the long pause question.
    The input area must render only Approve / Stop, not the question text.
    """
    cap_responses: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="list_files", args={"path": "."}) for _ in range(11)
    ]
    client, _ = make_test_app(cap_responses)
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    body = r.text
    input_section = _slice_input_area(body)

    # The orchestrator-generated continuation question typically contains
    # "iterations" or "continue". Neither should appear in the input area.
    assert "iterations without approval" not in input_section
    assert "Continue?" not in input_section
    # Buttons still present.
    assert "Approve" in input_section
    assert "Stop" in input_section


def test_input_area_does_not_restate_freeform_question(make_test_app):
    """For a freeform ask_user the chat bubble carries the question.
    The input area must drop the `Question: ...` restatement.
    """
    question_text = "What is your business name?"
    client, _ = make_test_app(
        [
            ToolCall(tool="ask_user", args={"question": question_text}),
            Final(summary="done"),
        ]
    )
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    body = r.text
    input_section = _slice_input_area(body)

    # The question text must appear in the chat bubble above (somewhere in
    # body) but NOT in the input area itself.
    assert question_text in body
    assert question_text not in input_section, input_section[:1500]
    # Old "Question:" label restatement is gone too.
    assert "Question:</strong>" not in input_section
    assert "<strong>Question:" not in input_section


def test_input_area_freeform_with_options_keeps_buttons_and_drops_question(make_test_app):
    """Freeform ask_user WITH options: option buttons stay, but the question
    restatement is gone."""
    question_text = "Which aesthetic do you prefer?"
    client, _ = make_test_app(
        [
            ToolCall(
                tool="ask_user",
                args={
                    "question": question_text,
                    "options": ["modern", "rustic"],
                },
            ),
            Final(summary="done"),
        ]
    )
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    body = r.text
    input_section = _slice_input_area(body)

    assert question_text not in input_section
    # Option buttons are still rendered.
    assert 'data-option="modern"' in input_section
    assert 'data-option="rustic"' in input_section
    # Other… button is rendered alongside.
    assert "other-btn" in input_section
    assert 'data-action="reveal-other"' in input_section


# ---------------------------------------------------------------------------
# 16. "Other…" JS does not hide the options row
# ---------------------------------------------------------------------------


def test_other_button_js_does_not_hide_options(make_test_app):
    """Regression guard: the JS handler for the Other… button must NOT
    hide ``.options-row`` — the user should still see the canned options
    after revealing the freeform textarea.
    """
    client, _ = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    # No JS line that hides options-row.
    assert "optsRow.style.display = 'none'" not in body
    assert "options-row" not in body or "style.display = 'none'" not in body.split("options-row", 1)[1]
    # The reveal handler is still present so Other… still works.
    assert 'data-action="reveal-other"' in body or "reveal-other" in body


# ---------------------------------------------------------------------------
# 17. request_approval bubble renders a subject-aware card, not raw JSON
# ---------------------------------------------------------------------------


def test_session_view_renders_brief_approval_card(make_test_app):
    """End-to-end: a request_approval(subject='business_brief', details=...)
    turn must produce a chat bubble containing the approval card markup
    (heading + at least one brief field) and NOT a raw JSON dump or a
    ``<details>`` expander wrapping the brief.
    """
    brief = {
        "name": "Maria's Pizzeria",
        "industry": "restaurant",
        "tagline": "Wood-fired pizza, made by hand.",
        "pages": ["Home", "Menu"],
    }
    responses: list[ToolCall | Final | Escalate] = [
        ToolCall(
            tool="request_approval",
            args={"subject": "business_brief", "details": brief},
        ),
        Final(summary="never reached in this test"),
    ]
    client, _ = make_test_app(responses)
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    chat_start = body.find('id="chat-log"')
    chat_end = body.find('id="chat-input"', chat_start)
    chat_section = body[chat_start:chat_end]

    # Card markup is present.
    assert '<div class="approval-card">' in chat_section
    assert '<h3 class="approval-subject">Business Brief</h3>' in chat_section
    assert "<dt>Industry</dt>" in chat_section
    # The brief is NOT dumped as a JSON pre-block or wrapped in <details>.
    assert "<pre>" not in chat_section
    # No `<details>` wrapping the approval JSON in the chat section.
    assert "Approval details</summary>" not in chat_section


# ---------------------------------------------------------------------------
# 18. render_mockup chat bubble renders a sandboxed iframe preview
# ---------------------------------------------------------------------------


def test_session_view_renders_mockup_iframe(make_test_app):
    """End-to-end: a render_mockup tool_call must produce a chat bubble
    containing a sandboxed iframe with a srcdoc attribute carrying the
    themed HTML mockup. No script execution path — sandbox="" is fully
    restrictive.
    """
    responses: list[ToolCall | Final | Escalate] = [
        ToolCall(
            tool="render_mockup",
            args={
                "layout_spec": {
                    "sections": [
                        {"name": "Header"},
                        {"name": "Hero"},
                        {"name": "Menu"},
                        {"name": "Footer"},
                    ],
                    "primary_cta": "Reserve",
                }
            },
        ),
        Final(summary="mockup rendered"),
    ]
    client, _ = make_test_app(responses)
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    chat_start = body.find('id="chat-log"')
    chat_end = body.find('id="chat-input"', chat_start)
    chat_section = body[chat_start:chat_end]

    # The iframe preview must be present in the chat panel.
    assert "<iframe" in chat_section
    assert 'sandbox=""' in chat_section
    assert "srcdoc=" in chat_section
    # The caption renders inside the bubble; the iframe is hoisted into
    # the full-width attachment div under the bubble.
    assert "mockup-card-caption" in chat_section
    assert "chat-attachment" in chat_section
    # No raw <pre> dump of the HTML — it lives inside the srcdoc attribute.
    assert "<pre>" not in chat_section


# ---------------------------------------------------------------------------
# 18b. save_business_brief chat bubble renders the brief as a labeled card
# ---------------------------------------------------------------------------


def test_session_view_save_business_brief_renders_card(make_test_app):
    """End-to-end: a save_business_brief tool_call must produce a chat bubble
    containing the labeled approval-card markup (heading + brief-name +
    brief-rows) — NOT a JSON dump of the brief dict. The card is the same
    layout as the request_approval brief card with a "Saving" heading so
    the user can tell the persist call apart from an approve request.
    """
    brief = {
        "name": "Jerry's HVAC",
        "industry": "Home service",
        "pages": ["Home", "Services"],
    }
    responses: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="save_business_brief", args={"brief": brief}),
        Final(summary="brief saved"),
    ]
    client, _ = make_test_app(responses)
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    chat_start = body.find('id="chat-log"')
    chat_end = body.find('id="chat-input"', chat_start)
    chat_section = body[chat_start:chat_end]

    # Card markup is present with the "Saving" heading.
    assert '<div class="approval-card">' in chat_section
    assert '<h3 class="approval-subject">Saving Business Brief</h3>' in chat_section
    assert "<dt>Industry</dt>" in chat_section
    assert "Home service" in chat_section
    # No raw JSON dump of the brief in the chat panel.
    assert "<pre>" not in chat_section
    assert '"brief":' not in chat_section
    assert '{"name":' not in chat_section


# ---------------------------------------------------------------------------
# 19. Events table renders a Rewind button on awaiting_human rows only
# ---------------------------------------------------------------------------


def test_events_table_has_rewind_button_on_awaiting_human_rows(make_test_app):
    """Drive a session to an awaiting_human pause; GET /view; assert the
    events table HTML contains a <button class="rewind-btn" data-event-id="...">
    only on awaiting_human rows.
    """
    client, _ = make_test_app(
        [
            ToolCall(tool="ask_user", args={"question": "first?"}),
            Final(summary="done"),
        ]
    )
    sid = _create_session(client)
    r = client.post(f"/sessions/{sid}/resume", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    # Slice the events table region so we only look at button placement there.
    events_start = body.find('id="events-card"')
    assert events_start != -1
    cost_start = body.find('id="cost-card"', events_start)
    events_section = body[events_start:cost_start]

    # There must be at least one rewind-btn with a non-empty data-event-id.
    assert 'class="rewind-btn"' in events_section
    m = re.search(
        r'<button[^>]*class="rewind-btn"[^>]*data-event-id="([^"]+)"',
        events_section,
    )
    assert m is not None, events_section[-2000:]
    assert m.group(1), "data-event-id must not be empty"

    # The Rewind button must NOT appear on non-awaiting_human rows. Count
    # awaiting_human event rows; the count of rewind-btn occurrences must
    # equal the count of awaiting_human row markers.
    awaiting_row_count = events_section.count("event-awaiting_human")
    rewind_count = events_section.count('class="rewind-btn"')
    assert rewind_count == awaiting_row_count, (
        f"expected {awaiting_row_count} rewind buttons, got {rewind_count}"
    )


# ---------------------------------------------------------------------------
# 20. After POST /rewind, the chat log renders a `rewound` divider
# ---------------------------------------------------------------------------


def test_session_view_renders_llm_calls_section(make_test_app):
    """The Details accordion includes an LLM calls section.

    On a freshly created session with no LLM calls, the empty-state copy
    ("No LLM calls yet.") must render. After seeding one llm_calls row
    directly via the store, the table row renders with the model + status.
    """
    client, data_dir = make_test_app()
    sid = _create_session(client)

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text
    # Empty-state path.
    assert "LLM calls" in body
    assert "No LLM calls yet." in body

    # Now seed one row directly and re-fetch.
    sessions_dir = data_dir / "sessions"
    conn = store.session_connection(sessions_dir, sid)
    try:
        store.record_llm_call(
            conn,
            model="qwen/qwen3-coder:free",
            is_fallback=False,
            request_messages=[
                {"role": "user", "content": "build a homepage"},
            ],
            request_options=None,
            response_text='{"type":"final","summary":"ok"}',
            finish_reason=None,
            tokens_in=4,
            tokens_out=9,
            cost_usd=0.0,
            status="ok",
        )
    finally:
        conn.close()

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text
    # The section header reflects the new count.
    assert "LLM calls (1)" in body
    # Model id (or its tail) and status appear in the table row.
    assert "qwen3-coder" in body
    assert ">ok<" in body
    # The expandable detail "View" summary is present.
    assert "<summary>View</summary>" in body


def test_rewound_divider_renders_in_chat(make_test_app):
    """Drive a session past an awaiting_human pause, rewind, GET /view, and
    assert a `.chat-divider` with "Rewound" text appears in the chat log.
    """
    client, _ = make_test_app(
        [
            ToolCall(tool="ask_user", args={"question": "Q1"}),
            ToolCall(tool="ask_user", args={"question": "Q2"}),
            Final(summary="done"),
        ]
    )
    sid = _create_session(client)
    client.post(f"/sessions/{sid}/resume", json={})

    detail = client.get(f"/sessions/{sid}").json()
    first_awaiting = next(
        e for e in detail["events"] if e["type"] == "awaiting_human"
    )
    pending_id = detail["pending_materials"][0]["id"]

    # Drive forward to a second pause.
    client.post(
        f"/sessions/{sid}/answer",
        json={"material_id": pending_id, "answer_text": "Alice"},
    )

    # Now rewind to the first pause.
    r = client.post(
        f"/sessions/{sid}/rewind",
        json={"target_event_id": first_awaiting["id"]},
    )
    assert r.status_code == 200, r.text

    r = client.get(f"/sessions/{sid}/view")
    assert r.status_code == 200
    body = r.text

    chat_start = body.find('id="chat-log"')
    chat_end = body.find('id="chat-input"', chat_start)
    chat_section = body[chat_start:chat_end]

    assert "chat-divider" in chat_section
    assert "Rewound" in chat_section
