"""Step 7 gate — the milestone test.

Drives a scripted 6-turn restaurant session through the orchestrator using
MockWorker so the loop is provably correct end-to-end on fixtures, with no
real LLM calls. Demonstrates all four rubric pillars (Guardrails, Checkpoints,
Material, Alarms) + the Worker pillar.

Test set:
  - test_milestone_full_restaurant_flow — the big scripted run.
  - test_milestone_at_least_one_alarm_engineered — confirms a real alarm row.
  - test_crash_resume_matches_uninterrupted — crash mid-loop, restart,
    assert identical terminal state.
  - test_alarm_iteration_limit_reached — preset iter_since_approval=cap.
  - test_alarm_spend_cap_reached — insert spend rows summing >= cap.
  - test_alarm_output_schema_violation — MockWorker returns a non-envelope.
  - test_alarm_tool_failed_denied_tool — ToolCall to a tool not in allow_list.
  - test_noop_resume_on_completed_session — second call returns immediately.

Helpers (`_simulate_human_answer`, `_simulate_human_approval`) mimic the
Step 9 `/answer` endpoint contract: persist a user_answer / user_approval
material, mark the prior pending material resolved, append a human_resumed
event, flip session status to 'active'. These belong to the API layer (not
store.py) and live inline here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.models.enums import (
    AlarmType,
    CheckpointName,
    CheckpointStatus,
    Direction,
    EventType,
    MaterialType,
    SessionStatus,
    Stage,
)
from harness.models.envelope import Escalate, Final, ToolCall, WorkerContext
from harness.services import store
from harness.services.orchestrator import (
    OrchestratorConfig,
    PAUSE_AWAITING_HUMAN,
    PAUSE_OUTPUT_SCHEMA,
    PAUSE_SPEND_CAP,
    PAUSE_TOOL_FAILED,
    PAUSE_TURN_CAP,
    run_until_pause,
)
from harness.services.worker import MockWorker


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


_DEFAULT_ALLOW = [
    "ask_user",
    "request_approval",
    "render_mockup",
    "read_file",
    "write_file",
    "list_files",
]


def _build_config(
    *,
    core_db_path: Path,
    sessions_dir: Path,
    sandbox_root: Path,
    workers_by_stage: dict[str, MockWorker],
    allow_list: list[str] | None = None,
    turn_cap: int = 10,
    spend_cap_usd: float = 1.0,
) -> OrchestratorConfig:
    """Wire a single MockWorker per stage. Reused by every sub-test."""

    def worker_for_stage(stage: str) -> MockWorker:
        if stage not in workers_by_stage:
            raise KeyError(f"no MockWorker configured for stage {stage!r}")
        return workers_by_stage[stage]

    def sandbox_root_for(session_id: str) -> Path:
        return sandbox_root / session_id

    return OrchestratorConfig(
        worker_for_stage=worker_for_stage,  # type: ignore[arg-type]
        system_prompt="You are the website builder agent.",
        allow_list=list(allow_list if allow_list is not None else _DEFAULT_ALLOW),
        sandbox_root_for=sandbox_root_for,
        core_db_path=core_db_path,
        sessions_dir=sessions_dir,
        turn_cap=turn_cap,
        spend_cap_usd=spend_cap_usd,
    )


def _seed_brief(session_conn: sqlite3.Connection, *, stage: str = "bootstrap") -> str:
    """Persist a business_brief material exactly as the bootstrap flow would."""
    return store.persist_material(
        session_conn,
        direction=Direction.OUT.value,
        stage=stage,
        type=MaterialType.BUSINESS_BRIEF.value,
        content={
            "name": "Maria's Pizzeria",
            "industry": "restaurant",
            "pages": ["Home", "Menu", "Contact"],
            "contact": "hello@marias.example",
        },
    )


def _last_awaiting_pending_material_id(session_conn: sqlite3.Connection) -> str:
    """Pull the pending material referenced by the latest awaiting_human event."""
    events = store.load_events(session_conn)
    for e in reversed(events):
        if e["type"] == EventType.AWAITING_HUMAN.value:
            mid = e["payload"]["material_id"]
            assert isinstance(mid, str)
            return mid
    raise AssertionError("no awaiting_human event present")


def _simulate_human_answer(
    core_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    session_id: str,
    *,
    pending_material_id: str,
    answer_text: str,
    stage: str,
) -> str:
    """Mimic POST /sessions/{id}/answer for an ask_user response."""
    answer_mid = store.persist_material(
        session_conn,
        direction=Direction.IN.value,
        stage=stage,
        type=MaterialType.USER_ANSWER.value,
        content={"answer_text": answer_text},
    )
    store.mark_material_resolved(session_conn, pending_material_id)
    store.append_event(
        session_conn,
        type=EventType.HUMAN_RESUMED.value,
        stage=stage,
        payload={
            "material_id": answer_mid,
            "answer_or_decision": {"answer_text": answer_text},
        },
        material_id=answer_mid,
    )
    store.update_session_status(
        core_conn, session_id, SessionStatus.ACTIVE.value
    )
    return answer_mid


def _simulate_human_approval(
    core_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    session_id: str,
    *,
    pending_material_id: str,
    approved: bool,
    subject: str,
    stage: str,
    notes: str | None = None,
) -> str:
    """Mimic POST /sessions/{id}/answer for a request_approval response."""
    content = {
        "approved": approved,
        "subject": subject,
        "kind": "approval",
        "notes": notes,
    }
    approval_mid = store.persist_material(
        session_conn,
        direction=Direction.IN.value,
        stage=stage,
        type=MaterialType.USER_APPROVAL.value,
        content=content,
    )
    store.mark_material_resolved(session_conn, pending_material_id)
    store.append_event(
        session_conn,
        type=EventType.HUMAN_RESUMED.value,
        stage=stage,
        payload={
            "material_id": approval_mid,
            "answer_or_decision": content,
        },
        material_id=approval_mid,
    )
    store.update_session_status(
        core_conn, session_id, SessionStatus.ACTIVE.value
    )
    return approval_mid


@pytest.fixture
def orchestrator_setup(tmp_path: Path):
    """Build a core DB + sessions dir + sandbox root for orchestrator tests.

    Yields (core_conn, sessions_dir, sandbox_root, session_id, core_db_path,
    session_conn_factory). The test owns lifetime; session_conn_factory
    re-opens the per-session DB when assertions need fresh row state.
    """
    core_db_path = tmp_path / "harness.db"
    sessions_dir = tmp_path / "sessions"
    sandbox_root = tmp_path / "sandboxes"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sandbox_root.mkdir(parents=True, exist_ok=True)

    core_conn = store.core_connection(core_db_path)
    session_id = store.create_session(core_conn)

    def session_conn_factory() -> sqlite3.Connection:
        return store.session_connection(sessions_dir, session_id)

    try:
        yield {
            "core_conn": core_conn,
            "core_db_path": core_db_path,
            "sessions_dir": sessions_dir,
            "sandbox_root": sandbox_root,
            "session_id": session_id,
            "session_conn_factory": session_conn_factory,
        }
    finally:
        core_conn.close()


# ---------------------------------------------------------------------------
# Milestone scripted run
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

_BAD_HTML = "<html><body><p>oops no title</p></body></html>"


def _restaurant_scripted_responses() -> list[ToolCall | Final | Escalate]:
    """Six scripted MockWorker responses driving the restaurant flow.

    The test setup seeds a business_brief material first, so the worker's
    first action is request_approval on the brief (turn 1). Subsequent
    turns: render_mockup, request_approval on mockup, write_file (bad),
    write_file (good), Final.
    """
    return [
        ToolCall(
            tool="request_approval",
            args={
                "subject": "business_brief",
                "details": {
                    "name": "Maria's Pizzeria",
                    "industry": "restaurant",
                    "pages": ["Home", "Menu", "Contact"],
                },
            },
        ),
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
        ToolCall(
            tool="request_approval",
            args={"subject": "mockup", "details": {"note": "ready for review"}},
        ),
        ToolCall(
            tool="write_file",
            args={"path": "index.html", "content": _BAD_HTML},
        ),
        ToolCall(
            tool="write_file",
            args={"path": "index.html", "content": _GOOD_HTML},
        ),
        Final(summary="Site built and validated."),
    ]


def test_milestone_full_restaurant_flow(orchestrator_setup):
    """The major milestone: full mock-driven session, all 4 pillars visible."""
    setup = orchestrator_setup
    session_id = setup["session_id"]

    # Seed the brief upfront — the bootstrap collect-info loop is out of
    # scope for this test (no persist_brief tool in v1).
    with setup["session_conn_factory"]() as session_conn:
        _seed_brief(session_conn)

    scripted = _restaurant_scripted_responses()
    worker = MockWorker(scripted_responses=scripted)
    workers_by_stage = {
        Stage.BOOTSTRAP.value: worker,
        Stage.MOCKUP.value: worker,
        Stage.BUILD.value: worker,
    }
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
    )

    # Turn 1: request_approval on the brief.
    r1 = run_until_pause(session_id, config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value
    assert r1.paused_reason == PAUSE_AWAITING_HUMAN
    assert r1.current_stage == Stage.BOOTSTRAP.value

    # Resume: approve the brief. Stage should advance to mockup, then the
    # next worker turn (render_mockup) runs and the orchestrator passes the
    # mockup_renders checkpoint.
    with setup["session_conn_factory"]() as session_conn:
        pending = _last_awaiting_pending_material_id(session_conn)
        _simulate_human_approval(
            setup["core_conn"],
            session_conn,
            session_id,
            pending_material_id=pending,
            approved=True,
            subject="business_brief",
            stage=Stage.BOOTSTRAP.value,
        )

    # Turn 2 + 3: render_mockup -> request_approval on mockup.
    r2 = run_until_pause(session_id, config)
    assert r2.status == SessionStatus.AWAITING_HUMAN.value
    assert r2.paused_reason == PAUSE_AWAITING_HUMAN
    # The brief-approval fold has already advanced to mockup; render_mockup
    # and request_approval both fired on the mockup stage.
    assert r2.current_stage == Stage.MOCKUP.value

    # Resume: approve the mockup. Stage advances to build.
    with setup["session_conn_factory"]() as session_conn:
        pending = _last_awaiting_pending_material_id(session_conn)
        _simulate_human_approval(
            setup["core_conn"],
            session_conn,
            session_id,
            pending_material_id=pending,
            approved=True,
            subject="mockup",
            stage=Stage.MOCKUP.value,
        )

    # Turns 4 + 5 + 6: write_file (bad) -> post-hooks fail -> write_file (good)
    # -> post-hooks pass -> Final.
    r3 = run_until_pause(session_id, config)
    assert r3.status == SessionStatus.COMPLETED.value
    assert r3.terminal is True
    assert r3.current_stage == Stage.DONE.value

    # ------------------------------------------------------------------
    # Assertions: pillars + event/checkpoint completeness.
    # ------------------------------------------------------------------
    with setup["session_conn_factory"]() as session_conn:
        events = store.load_events(session_conn)
        ckpts = store.load_checkpoints(session_conn)

    event_types = {e["type"] for e in events}
    required_event_types = {
        EventType.WORKER_INPUT.value,
        EventType.WORKER_OUTPUT.value,
        EventType.TOOL_CALL.value,
        EventType.TOOL_RESULT.value,
        EventType.POST_HOOK_RUN.value,
        EventType.CHECKPOINT_RESULT.value,
        EventType.AWAITING_HUMAN.value,
        EventType.HUMAN_RESUMED.value,
    }
    missing = required_event_types - event_types
    assert not missing, f"missing event types: {missing}"

    # All 5 checkpoint names must have at least one row.
    names_present = {c["name"] for c in ckpts}
    expected_names = {
        CheckpointName.BUSINESS_BRIEF_CONFIRMED.value,
        CheckpointName.MOCKUP_RENDERS.value,
        CheckpointName.MOCKUP_APPROVED.value,
        CheckpointName.SITE_VALID.value,
        CheckpointName.SEO_ARTIFACTS_PRESENT.value,
    }
    assert expected_names <= names_present, f"missing checkpoints: {expected_names - names_present}"

    # First site_valid fails, second passes.
    site_valid_rows = [
        c for c in ckpts if c["name"] == CheckpointName.SITE_VALID.value
    ]
    assert len(site_valid_rows) >= 2
    assert site_valid_rows[0]["status"] == CheckpointStatus.FAIL.value
    assert site_valid_rows[-1]["status"] == CheckpointStatus.PASS.value

    # seo_artifacts_present must pass at least once.
    seo_rows = [
        c
        for c in ckpts
        if c["name"] == CheckpointName.SEO_ARTIFACTS_PRESENT.value
    ]
    assert any(c["status"] == CheckpointStatus.PASS.value for c in seo_rows)

    # Session terminal state.
    final_session = store.load_session(setup["core_conn"], session_id)
    assert final_session is not None
    assert final_session["status"] == SessionStatus.COMPLETED.value
    assert final_session["current_stage"] == Stage.DONE.value

    # Stage advancement visible across the events.
    stages_seen = [e["stage"] for e in events]
    assert Stage.BOOTSTRAP.value in stages_seen
    assert Stage.MOCKUP.value in stages_seen
    assert Stage.BUILD.value in stages_seen

    # Generated files actually exist in the sandbox.
    sandbox = setup["sandbox_root"] / session_id
    assert (sandbox / "index.html").is_file()
    assert (sandbox / "sitemap.xml").is_file()
    assert (sandbox / "robots.txt").is_file()
    assert (sandbox / "llms.txt").is_file()

    # All 6 scripted responses consumed.
    assert len(worker.received_contexts) == 6


def test_milestone_at_least_one_alarm_row(orchestrator_setup):
    """Sub-test of the milestone — engineer a denied-tool ToolCall so the
    alarms table acquires at least one row in a flow that touches every
    other pillar.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]

    # Worker tries a tool that exists but is not on the allow-list.
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="read_file", args={"path": "anything.html"}),
        Final(summary="caught the denial."),
    ]
    worker = MockWorker(scripted_responses=scripted)
    workers_by_stage = {
        Stage.BOOTSTRAP.value: worker,
        Stage.MOCKUP.value: worker,
        Stage.BUILD.value: worker,
    }
    # Allow-list deliberately omits read_file.
    restricted_allow = ["ask_user", "request_approval", "render_mockup"]
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        allow_list=restricted_allow,
    )

    result = run_until_pause(session_id, config)
    # After the tool fails, the loop runs Final on the same call (no pause).
    assert result.status == SessionStatus.COMPLETED.value

    with setup["session_conn_factory"]() as session_conn:
        alarms_rows = store.load_alarms(session_conn)

    assert len(alarms_rows) >= 1
    assert alarms_rows[0]["type"] == AlarmType.TOOL_FAILED.value
    assert alarms_rows[0]["context"]["tool"] == "read_file"
    assert alarms_rows[0]["context"]["error_kind"] == "denied_by_allowlist"


# ---------------------------------------------------------------------------
# Crash-resume
# ---------------------------------------------------------------------------


def test_crash_resume_matches_uninterrupted(tmp_path: Path):
    """Stop mid-loop, re-open connections, complete with remaining responses.

    The terminal state (session row + checkpoint set + final site_valid
    status) must match a single-run baseline.
    """

    def run_full(label: str) -> dict:
        root = tmp_path / label
        core_db_path = root / "harness.db"
        sessions_dir = root / "sessions"
        sandbox_root = root / "sandboxes"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sandbox_root.mkdir(parents=True, exist_ok=True)

        core_conn = store.core_connection(core_db_path)
        session_id = store.create_session(core_conn)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            _seed_brief(session_conn)

        scripted = _restaurant_scripted_responses()
        worker = MockWorker(scripted_responses=scripted)
        workers_by_stage = {
            Stage.BOOTSTRAP.value: worker,
            Stage.MOCKUP.value: worker,
            Stage.BUILD.value: worker,
        }
        config = _build_config(
            core_db_path=core_db_path,
            sessions_dir=sessions_dir,
            sandbox_root=sandbox_root,
            workers_by_stage=workers_by_stage,
        )

        # Pause 1: brief approval
        run_until_pause(session_id, config)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            pending = _last_awaiting_pending_material_id(session_conn)
            _simulate_human_approval(
                core_conn,
                session_conn,
                session_id,
                pending_material_id=pending,
                approved=True,
                subject="business_brief",
                stage=Stage.BOOTSTRAP.value,
            )
        # Pause 2: mockup approval
        run_until_pause(session_id, config)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            pending = _last_awaiting_pending_material_id(session_conn)
            _simulate_human_approval(
                core_conn,
                session_conn,
                session_id,
                pending_material_id=pending,
                approved=True,
                subject="mockup",
                stage=Stage.MOCKUP.value,
            )
        # Run to completion.
        run_until_pause(session_id, config)

        final_session = store.load_session(core_conn, session_id)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            ckpts = store.load_checkpoints(session_conn)
        core_conn.close()
        return {
            "session": final_session,
            "checkpoints": [(c["name"], c["status"]) for c in ckpts],
        }

    def run_with_crash() -> dict:
        """Same path, but each run_until_pause hits a fresh process (we
        simulate by closing core_conn between calls and reopening for each
        resume; the orchestrator opens its own connections internally)."""
        root = tmp_path / "crash"
        core_db_path = root / "harness.db"
        sessions_dir = root / "sessions"
        sandbox_root = root / "sandboxes"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sandbox_root.mkdir(parents=True, exist_ok=True)

        # Create session in a short-lived connection (simulates "process A").
        core_conn = store.core_connection(core_db_path)
        session_id = store.create_session(core_conn)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            _seed_brief(session_conn)
        core_conn.close()

        scripted = _restaurant_scripted_responses()
        # All six scripted responses are queued upfront; the orchestrator
        # would normally see them across multiple calls. A "crash" between
        # calls is simulated by tearing down + rebuilding workers and
        # connections — but the WORKER also has to remember its queue.
        # That mirrors reality: a real LLMWorker is stateless across
        # processes; the message history rebuilds it. For MockWorker we
        # reuse the same instance so the queue carries through.
        worker = MockWorker(scripted_responses=scripted)
        workers_by_stage = {
            Stage.BOOTSTRAP.value: worker,
            Stage.MOCKUP.value: worker,
            Stage.BUILD.value: worker,
        }
        config = OrchestratorConfig(
            worker_for_stage=lambda s: workers_by_stage[s],  # type: ignore[arg-type]
            system_prompt="You are the website builder agent.",
            allow_list=list(_DEFAULT_ALLOW),
            sandbox_root_for=lambda sid: sandbox_root / sid,
            core_db_path=core_db_path,
            sessions_dir=sessions_dir,
        )

        # First leg: drive turn 1, pause for brief approval, then approve.
        run_until_pause(session_id, config)
        # *** simulate a crash here — re-open everything fresh ***
        core_conn = store.core_connection(core_db_path)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            pending = _last_awaiting_pending_material_id(session_conn)
            _simulate_human_approval(
                core_conn,
                session_conn,
                session_id,
                pending_material_id=pending,
                approved=True,
                subject="business_brief",
                stage=Stage.BOOTSTRAP.value,
            )
        core_conn.close()

        # Second leg: turns 2-3 (mockup), pause, approve.
        run_until_pause(session_id, config)
        # *** simulate another crash ***
        core_conn = store.core_connection(core_db_path)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            pending = _last_awaiting_pending_material_id(session_conn)
            _simulate_human_approval(
                core_conn,
                session_conn,
                session_id,
                pending_material_id=pending,
                approved=True,
                subject="mockup",
                stage=Stage.MOCKUP.value,
            )
        core_conn.close()

        # Final leg: write_file bad -> write_file good -> Final.
        run_until_pause(session_id, config)

        core_conn = store.core_connection(core_db_path)
        final_session = store.load_session(core_conn, session_id)
        with store.session_connection(sessions_dir, session_id) as session_conn:
            ckpts = store.load_checkpoints(session_conn)
        core_conn.close()
        return {
            "session": final_session,
            "checkpoints": [(c["name"], c["status"]) for c in ckpts],
        }

    baseline = run_full("baseline")
    crashed = run_with_crash()

    assert baseline["session"] is not None and crashed["session"] is not None
    assert baseline["session"]["status"] == crashed["session"]["status"]
    assert (
        baseline["session"]["current_stage"]
        == crashed["session"]["current_stage"]
    )
    assert baseline["checkpoints"] == crashed["checkpoints"]


# ---------------------------------------------------------------------------
# All four alarm types raisable
# ---------------------------------------------------------------------------


def test_alarm_iteration_limit_reached(orchestrator_setup):
    setup = orchestrator_setup
    session_id = setup["session_id"]
    # Force the cap at session entry.
    store.update_session_status(
        setup["core_conn"], session_id, SessionStatus.ACTIVE.value, iter_since_approval=10
    )
    worker = MockWorker(scripted_responses=[Final(summary="unused")])
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        turn_cap=10,
    )

    result = run_until_pause(session_id, config)
    assert result.status == SessionStatus.AWAITING_HUMAN.value
    assert result.paused_reason == PAUSE_TURN_CAP

    with setup["session_conn_factory"]() as session_conn:
        rows = store.load_alarms(session_conn)
    assert any(
        r["type"] == AlarmType.ITERATION_LIMIT_REACHED.value for r in rows
    )


def test_alarm_spend_cap_reached(orchestrator_setup):
    setup = orchestrator_setup
    session_id = setup["session_id"]
    # Insert spend rows totaling >= cap in the last 24h.
    now = datetime.now(timezone.utc)
    store.record_spend(
        setup["core_conn"],
        session_id,
        model="deepseek/deepseek-v4-flash",
        tokens_in=1000,
        tokens_out=200,
        cost_usd=0.6,
        ts=now - timedelta(minutes=10),
    )
    store.record_spend(
        setup["core_conn"],
        session_id,
        model="qwen/qwen3-coder",
        tokens_in=2000,
        tokens_out=400,
        cost_usd=0.5,
        ts=now - timedelta(minutes=5),
    )

    worker = MockWorker(scripted_responses=[Final(summary="unused")])
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
    )

    result = run_until_pause(session_id, config)
    assert result.status == SessionStatus.AWAITING_HUMAN.value
    assert result.paused_reason == PAUSE_SPEND_CAP

    with setup["session_conn_factory"]() as session_conn:
        rows = store.load_alarms(session_conn)
    spend_rows = [r for r in rows if r["type"] == AlarmType.SPEND_CAP_REACHED.value]
    assert spend_rows, "spend_cap_reached alarm not raised"
    assert spend_rows[0]["context"]["window"] == "day"
    assert spend_rows[0]["context"]["cap_usd"] == 1.0


class _NonEnvelopeWorker:
    """Worker subclass that returns a string instead of a typed envelope."""

    def act(self, ctx: WorkerContext) -> object:
        return "not_a_real_response"


def test_alarm_output_schema_violation(orchestrator_setup):
    setup = orchestrator_setup
    session_id = setup["session_id"]
    bad_worker = _NonEnvelopeWorker()
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage={Stage.BOOTSTRAP.value: bad_worker},  # type: ignore[dict-item]
    )

    result = run_until_pause(session_id, config)
    assert result.status == SessionStatus.AWAITING_HUMAN.value
    assert result.paused_reason == PAUSE_OUTPUT_SCHEMA

    with setup["session_conn_factory"]() as session_conn:
        rows = store.load_alarms(session_conn)
    assert any(
        r["type"] == AlarmType.OUTPUT_SCHEMA_VIOLATION.value for r in rows
    )


def test_alarm_tool_failed_denied_tool(orchestrator_setup):
    """Documented behavior: dispatcher raises tool_failed, the orchestrator
    DOES NOT pause — it continues to the next turn. The Final on the next
    turn completes the session, giving us a clean assertion target.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="delete_file", args={"path": "anything"}),
        Final(summary="done after the failure"),
    ]
    worker = MockWorker(scripted_responses=scripted)
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
    )

    result = run_until_pause(session_id, config)
    assert result.status == SessionStatus.COMPLETED.value

    with setup["session_conn_factory"]() as session_conn:
        rows = store.load_alarms(session_conn)
    tf_rows = [r for r in rows if r["type"] == AlarmType.TOOL_FAILED.value]
    assert tf_rows, "no tool_failed alarm raised"
    assert tf_rows[0]["context"]["tool"] == "delete_file"
    assert tf_rows[0]["context"]["error_kind"] == "denied_by_allowlist"


# ---------------------------------------------------------------------------
# No-op resume on already-terminal session
# ---------------------------------------------------------------------------


def test_noop_resume_on_completed_session(orchestrator_setup):
    setup = orchestrator_setup
    session_id = setup["session_id"]
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.COMPLETED.value,
        current_stage=Stage.DONE.value,
    )

    # Worker queue intentionally empty — if the orchestrator called act() it
    # would raise MockWorkerExhausted.
    worker = MockWorker(scripted_responses=[])
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage={Stage.DONE.value: worker, Stage.BOOTSTRAP.value: worker},
    )

    result = run_until_pause(session_id, config)
    assert result.status == SessionStatus.COMPLETED.value
    assert result.terminal is True
    assert result.turns_executed == 0

    # No events should have been written.
    with setup["session_conn_factory"]() as session_conn:
        events = store.load_events(session_conn)
    assert events == []
