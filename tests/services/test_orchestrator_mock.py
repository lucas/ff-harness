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
    force_continue,
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
        pending = store.load_pending_materials(session_conn)
    assert any(
        r["type"] == AlarmType.ITERATION_LIMIT_REACHED.value for r in rows
    )
    # A pending continuation_approval material should also be persisted so the
    # UI can render an "approve / stop" affordance for the operator.
    cont = [
        m
        for m in pending
        if m["type"] == MaterialType.PENDING_QUESTION.value
        and isinstance(m.get("content"), dict)
        and m["content"].get("kind") == "continuation_approval"
    ]
    assert len(cont) == 1
    assert cont[0]["content"].get("iter_count") == 10
    assert "autonomous iterations" in cont[0]["content"].get("question", "")


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


# ---------------------------------------------------------------------------
# Issue 1 + Issue 2 regression tests
# ---------------------------------------------------------------------------


def _last_pending_continuation_material_id(session_conn: sqlite3.Connection) -> str:
    """Find the most recent pending continuation_approval material."""
    pending = store.load_pending_materials(session_conn)
    for m in reversed(pending):
        if (
            m["type"] == MaterialType.PENDING_QUESTION.value
            and isinstance(m.get("content"), dict)
            and m["content"].get("kind") == "continuation_approval"
        ):
            return m["id"]
    raise AssertionError("no pending continuation_approval material present")


def test_iter_counter_resets_on_any_human_input(orchestrator_setup):
    """A run of N ask_user turns must NOT trip the iter cap.

    Each user_answer (kind != 'approval') is a human input and must reset
    iter_since_approval to 0 so the cap can never fire during an active
    Q&A bootstrap exchange.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]

    # 5 ask_user turns followed by a Final to terminate. Each ask_user pauses
    # the loop; the test drives an answer between pauses.
    n_questions = 5
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(
            tool="ask_user",
            args={"question": f"question {i + 1}?"},
        )
        for i in range(n_questions)
    ]
    scripted.append(Final(summary="done collecting answers."))

    worker = MockWorker(scripted_responses=scripted)
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        turn_cap=3,  # deliberately small — proves the counter resets.
    )

    for i in range(n_questions):
        result = run_until_pause(session_id, config)
        assert result.status == SessionStatus.AWAITING_HUMAN.value
        assert result.paused_reason == PAUSE_AWAITING_HUMAN, (
            f"unexpected pause reason at question {i + 1}: {result.paused_reason}"
        )
        # Right after an ask_user pause, the orchestrator bumped iter to
        # iter_count+1 (= 1 for the first turn of a freshly-reset counter).
        session_row = store.load_session(setup["core_conn"], session_id)
        assert session_row is not None
        assert int(session_row["iter_since_approval"]) <= 1, (
            f"iter_since_approval climbed to {session_row['iter_since_approval']}"
            f" after question {i + 1} — counter should have been reset"
        )

        with setup["session_conn_factory"]() as session_conn:
            pending_mid = _last_awaiting_pending_material_id(session_conn)
            _simulate_human_answer(
                setup["core_conn"],
                session_conn,
                session_id,
                pending_material_id=pending_mid,
                answer_text=f"answer {i + 1}",
                stage=Stage.BOOTSTRAP.value,
            )

        # After the simulated answer is appended (and before the next
        # run_until_pause), iter_since_approval has not yet been reset
        # (that happens inside the orchestrator's resume-folding step).
        # We assert the state AFTER the next run instead — see top of loop.

    # 6th call drains the Final. No iter cap should have tripped.
    final_result = run_until_pause(session_id, config)
    assert final_result.status == SessionStatus.COMPLETED.value
    assert final_result.terminal is True

    with setup["session_conn_factory"]() as session_conn:
        alarms_rows = store.load_alarms(session_conn)
    assert not any(
        r["type"] == AlarmType.ITERATION_LIMIT_REACHED.value
        for r in alarms_rows
    ), "iteration_limit_reached alarm fired despite continuous human input"


def test_continuation_approval_resumes_after_iter_cap(orchestrator_setup):
    """Trip the iter cap; approve the continuation; the loop must proceed."""
    setup = orchestrator_setup
    session_id = setup["session_id"]
    # Force the cap at session entry.
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.ACTIVE.value,
        iter_since_approval=10,
    )
    # After the cap-pause + user approval, the orchestrator should proceed
    # to the Final on the next loop iteration.
    worker = MockWorker(scripted_responses=[Final(summary="resumed after cap")])
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        turn_cap=10,
    )

    r1 = run_until_pause(session_id, config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value
    assert r1.paused_reason == PAUSE_TURN_CAP

    # Find + approve the continuation_approval pending material.
    with setup["session_conn_factory"]() as session_conn:
        pending_mid = _last_pending_continuation_material_id(session_conn)
        _simulate_human_approval(
            setup["core_conn"],
            session_conn,
            session_id,
            pending_material_id=pending_mid,
            approved=True,
            subject="continuation",  # unknown-subject path; counter still resets
            stage=Stage.BOOTSTRAP.value,
        )
        # Patch the persisted approval content to carry the continuation
        # kind so the orchestrator's denial-detection branch sees it on
        # subsequent runs. The helper hardcodes kind='approval', which is
        # fine for this APPROVED case (the orchestrator only checks for
        # kind == continuation_approval AND approved is False to deny).

    # Counter should be zero after the approval is folded in; loop completes.
    r2 = run_until_pause(session_id, config)
    assert r2.status == SessionStatus.COMPLETED.value
    assert r2.terminal is True

    session_row = store.load_session(setup["core_conn"], session_id)
    assert session_row is not None
    assert int(session_row["iter_since_approval"]) == 0


def test_continuation_approval_denial_keeps_session_paused(orchestrator_setup):
    """Denying the continuation_approval must keep status awaiting_human.

    Implemented policy (option A): when a continuation_approval is denied
    (approved=False), the orchestrator reverts session status to
    awaiting_human and does NOT reset the iter counter. Subsequent
    run_until_pause calls no-op until the operator does something else
    (e.g. manually approves a later continuation). No further worker
    turns are taken.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.ACTIVE.value,
        iter_since_approval=10,
    )
    # MockWorker with no scripted responses — if the orchestrator tried
    # to call act() after the denial, MockWorkerExhausted would fire and
    # surface a worker_exception alarm. The test asserts neither happens.
    worker = MockWorker(scripted_responses=[])
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        turn_cap=10,
    )

    r1 = run_until_pause(session_id, config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value
    assert r1.paused_reason == PAUSE_TURN_CAP

    # Deny the continuation. The helper uses kind='approval' by default;
    # we patch the persisted material to kind='continuation_approval' so
    # the orchestrator's denial branch matches.
    with setup["session_conn_factory"]() as session_conn:
        pending_mid = _last_pending_continuation_material_id(session_conn)
        approval_mid = _simulate_human_approval(
            setup["core_conn"],
            session_conn,
            session_id,
            pending_material_id=pending_mid,
            approved=False,
            subject="continuation",
            stage=Stage.BOOTSTRAP.value,
        )
        # Rewrite the material's content to carry the continuation_approval
        # kind, mirroring what the real /answer route does.
        import json as _json
        session_conn.execute(
            "UPDATE material SET content = ? WHERE id = ?",
            (
                _json.dumps(
                    {
                        "approved": False,
                        "subject": "continuation",
                        "kind": "continuation_approval",
                        "notes": None,
                    }
                ),
                approval_mid,
            ),
        )
        session_conn.commit()

    r2 = run_until_pause(session_id, config)
    # The denial reverted status to awaiting_human; the second run sees the
    # non-active session and returns immediately (no worker turns).
    assert r2.status == SessionStatus.AWAITING_HUMAN.value
    assert r2.turns_executed == 0

    # iter_since_approval was NOT reset by denial.
    session_row = store.load_session(setup["core_conn"], session_id)
    assert session_row is not None
    assert int(session_row["iter_since_approval"]) == 10

    # No tool_failed / worker_exception alarms were raised by the denial.
    with setup["session_conn_factory"]() as session_conn:
        alarms_rows = store.load_alarms(session_conn)
    tf = [r for r in alarms_rows if r["type"] == AlarmType.TOOL_FAILED.value]
    assert tf == [], f"unexpected tool_failed alarm: {tf}"


def test_spend_cap_persists_continuation_pending_material(orchestrator_setup):
    """Tripping the rolling-24h spend cap must persist a continuation_approval."""
    setup = orchestrator_setup
    session_id = setup["session_id"]
    now = datetime.now(timezone.utc)
    store.record_spend(
        setup["core_conn"],
        session_id,
        model="deepseek/deepseek-v4-flash",
        tokens_in=1000,
        tokens_out=200,
        cost_usd=0.7,
        ts=now - timedelta(minutes=10),
    )
    store.record_spend(
        setup["core_conn"],
        session_id,
        model="qwen/qwen3-coder",
        tokens_in=2000,
        tokens_out=400,
        cost_usd=0.4,
        ts=now - timedelta(minutes=5),
    )

    worker = MockWorker(scripted_responses=[Final(summary="unused")])
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        spend_cap_usd=1.0,
    )

    result = run_until_pause(session_id, config)
    assert result.status == SessionStatus.AWAITING_HUMAN.value
    assert result.paused_reason == PAUSE_SPEND_CAP

    with setup["session_conn_factory"]() as session_conn:
        pending = store.load_pending_materials(session_conn)
    cont = [
        m
        for m in pending
        if m["type"] == MaterialType.PENDING_QUESTION.value
        and isinstance(m.get("content"), dict)
        and m["content"].get("kind") == "continuation_approval"
    ]
    assert len(cont) == 1
    question = cont[0]["content"].get("question", "")
    # The question must reference the spent amount and the rolling-24h
    # caveat so the operator understands continuing won't bypass the cap.
    assert "$1.10" in question or "1.1" in question, (
        f"spend amount not in question: {question!r}"
    )
    assert "rolling-24h" in question or "rolling" in question


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


# ---------------------------------------------------------------------------
# State-based alarm auto-resolution
# ---------------------------------------------------------------------------


def test_continuation_approval_resolves_iter_alarm(orchestrator_setup):
    """Approving a continuation_approval resolves the iter-cap alarm.

    iteration_limit_reached is state-based: once the counter is reset (by
    a human resume) and falls below the cap, the alarm row must flip to
    resolved=1 on the next run_until_pause.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    # Force the cap at session entry.
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.ACTIVE.value,
        iter_since_approval=10,
    )
    worker = MockWorker(scripted_responses=[Final(summary="resumed after cap")])
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        turn_cap=10,
    )

    r1 = run_until_pause(session_id, config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value
    assert r1.paused_reason == PAUSE_TURN_CAP

    # Capture the freshly-raised iter alarm id (still unresolved).
    with setup["session_conn_factory"]() as session_conn:
        rows_before = store.load_alarms(session_conn)
    iter_rows = [
        r
        for r in rows_before
        if r["type"] == AlarmType.ITERATION_LIMIT_REACHED.value
    ]
    assert iter_rows, "expected an iteration_limit_reached alarm"
    assert all(r["resolved"] is False for r in iter_rows)
    target_id = iter_rows[-1]["id"]

    # Approve the continuation — counter resets to 0 on resume-fold.
    with setup["session_conn_factory"]() as session_conn:
        pending_mid = _last_pending_continuation_material_id(session_conn)
        _simulate_human_approval(
            setup["core_conn"],
            session_conn,
            session_id,
            pending_material_id=pending_mid,
            approved=True,
            subject="continuation",
            stage=Stage.BOOTSTRAP.value,
        )

    r2 = run_until_pause(session_id, config)
    assert r2.status == SessionStatus.COMPLETED.value

    with setup["session_conn_factory"]() as session_conn:
        rows_after = store.load_alarms(session_conn)
    alarm_after = next(r for r in rows_after if r["id"] == target_id)
    assert alarm_after["resolved"] is True, (
        "iteration_limit_reached alarm should auto-resolve after counter reset"
    )


def test_resume_without_continuation_leaves_alarm_unresolved(orchestrator_setup):
    """If the iter cap still holds on resume, the old alarm stays open and
    a new one is appended on the next trip.

    No continuation_approval is granted — the counter remains >= cap, so
    the state condition still holds. The original alarm must remain
    resolved=0, and the next loop iteration must raise a fresh alarm row.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.ACTIVE.value,
        iter_since_approval=10,
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

    r1 = run_until_pause(session_id, config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value
    assert r1.paused_reason == PAUSE_TURN_CAP

    with setup["session_conn_factory"]() as session_conn:
        rows_first = store.load_alarms(session_conn)
    iter_rows_first = [
        r
        for r in rows_first
        if r["type"] == AlarmType.ITERATION_LIMIT_REACHED.value
    ]
    assert len(iter_rows_first) == 1
    original_id = iter_rows_first[0]["id"]

    # Forge an active state WITHOUT user approval, leaving the counter at
    # the cap. The next run will re-trip the cap and append a new alarm.
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.ACTIVE.value,
        iter_since_approval=10,
    )

    r2 = run_until_pause(session_id, config)
    assert r2.status == SessionStatus.AWAITING_HUMAN.value
    assert r2.paused_reason == PAUSE_TURN_CAP

    with setup["session_conn_factory"]() as session_conn:
        rows_second = store.load_alarms(session_conn)
    iter_rows_second = [
        r
        for r in rows_second
        if r["type"] == AlarmType.ITERATION_LIMIT_REACHED.value
    ]
    # Original alarm should still be unresolved; a new alarm row appended.
    original_after = next(r for r in iter_rows_second if r["id"] == original_id)
    assert original_after["resolved"] is False
    assert len(iter_rows_second) >= 2, (
        "expected a new iteration_limit_reached alarm row to be appended"
    )
    new_rows = [r for r in iter_rows_second if r["id"] != original_id]
    assert any(r["resolved"] is False for r in new_rows)


def test_spend_cap_alarm_resolves_when_spend_drops(orchestrator_setup):
    """spend_cap_reached must auto-resolve when 24h spend drops below cap."""
    setup = orchestrator_setup
    session_id = setup["session_id"]

    # Seed >$1 of spend in the last 24h.
    now = datetime.now(timezone.utc)
    spend_id_a = store.record_spend(
        setup["core_conn"],
        session_id,
        model="deepseek/deepseek-v4-flash",
        tokens_in=1000,
        tokens_out=200,
        cost_usd=0.7,
        ts=now - timedelta(minutes=10),
    )
    spend_id_b = store.record_spend(
        setup["core_conn"],
        session_id,
        model="qwen/qwen3-coder",
        tokens_in=2000,
        tokens_out=400,
        cost_usd=0.4,
        ts=now - timedelta(minutes=5),
    )

    worker = MockWorker(
        scripted_responses=[Final(summary="proceeded after spend dropped")]
    )
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
        spend_cap_usd=1.0,
    )

    r1 = run_until_pause(session_id, config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value
    assert r1.paused_reason == PAUSE_SPEND_CAP

    with setup["session_conn_factory"]() as session_conn:
        rows_before = store.load_alarms(session_conn)
    spend_rows = [
        r for r in rows_before if r["type"] == AlarmType.SPEND_CAP_REACHED.value
    ]
    assert spend_rows and all(r["resolved"] is False for r in spend_rows)
    target_id = spend_rows[-1]["id"]

    # Drop the spend below the cap and flip the session back to active so
    # the loop will reach the resolve step.
    setup["core_conn"].execute(
        "DELETE FROM spend_log WHERE id IN (?, ?)", (spend_id_a, spend_id_b)
    )
    setup["core_conn"].commit()
    store.update_session_status(
        setup["core_conn"], session_id, SessionStatus.ACTIVE.value
    )

    r2 = run_until_pause(session_id, config)
    assert r2.status == SessionStatus.COMPLETED.value

    with setup["session_conn_factory"]() as session_conn:
        rows_after = store.load_alarms(session_conn)
    alarm_after = next(r for r in rows_after if r["id"] == target_id)
    assert alarm_after["resolved"] is True, (
        "spend_cap_reached alarm should auto-resolve when spend drops below cap"
    )


# ---------------------------------------------------------------------------
# force_continue — /resume unstick helper
# ---------------------------------------------------------------------------


def _seed_pending_continuation(
    session_conn: sqlite3.Connection,
    *,
    stage: str = Stage.BOOTSTRAP.value,
    iter_count: int = 10,
) -> str:
    """Persist a pending continuation_approval material directly (no loop)."""
    return store.persist_material(
        session_conn,
        direction=Direction.OUT.value,
        stage=stage,
        type=MaterialType.PENDING_QUESTION.value,
        content={
            "kind": "continuation_approval",
            "question": "Approve another 10 iterations?",
            "iter_count": iter_count,
        },
        pending=True,
    )


def _seed_pending_ask_user(
    session_conn: sqlite3.Connection,
    *,
    stage: str = Stage.BOOTSTRAP.value,
    question: str = "What is your business name?",
) -> str:
    """Persist a pending freeform ask_user question (no kind field)."""
    return store.persist_material(
        session_conn,
        direction=Direction.OUT.value,
        stage=stage,
        type=MaterialType.PENDING_QUESTION.value,
        content={"question": question},
        pending=True,
    )


def _seed_pending_approval(
    session_conn: sqlite3.Connection,
    *,
    subject: str = "business_brief",
    stage: str = Stage.BOOTSTRAP.value,
) -> str:
    """Persist a pending subject-based approval (e.g. business_brief / mockup)."""
    return store.persist_material(
        session_conn,
        direction=Direction.OUT.value,
        stage=stage,
        type=MaterialType.PENDING_QUESTION.value,
        content={
            "kind": "approval",
            "subject": subject,
            "details": {"name": "Maria's Pizzeria"},
        },
        pending=True,
    )


def test_force_continue_auto_approves_continuation_pending(orchestrator_setup):
    """force_continue must auto-approve a pending continuation_approval.

    Asserts:
      - the pending row is resolved
      - a user_approval material with auto_approved_via_resume=True exists
      - a human_resumed event references the new approval material
      - session is active with iter_since_approval=0
      - report.auto_approved == 1
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    # Pre-set the session to awaiting_human with a non-zero iter counter to
    # mirror the real "stuck at the cap" state.
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.AWAITING_HUMAN.value,
        iter_since_approval=10,
    )
    with setup["session_conn_factory"]() as session_conn:
        pending_mid = _seed_pending_continuation(session_conn)

    with setup["session_conn_factory"]() as session_conn:
        report = force_continue(
            session_id,
            core_conn=setup["core_conn"],
            session_conn=session_conn,
        )

    assert report["auto_approved"] == 1
    assert report["previous_status"] == SessionStatus.AWAITING_HUMAN.value
    assert report["previous_iter"] == 10
    assert pending_mid not in report["pending_remaining"]

    with setup["session_conn_factory"]() as session_conn:
        original = store.load_material(session_conn, pending_mid)
        assert original is not None
        assert original["pending"] is False

        # A user_approval material with the auto-approve marker must exist.
        approvals = session_conn.execute(
            "SELECT id, content FROM material WHERE type = ?",
            (MaterialType.USER_APPROVAL.value,),
        ).fetchall()
        import json as _json

        decoded = [
            (r["id"], _json.loads(r["content"])) for r in approvals
        ]
        matches = [
            (mid, c)
            for mid, c in decoded
            if c.get("kind") == "continuation_approval"
            and c.get("approved") is True
            and c.get("auto_approved_via_resume") is True
        ]
        assert len(matches) == 1, f"expected one auto-approval, got {decoded}"
        approval_mid = matches[0][0]

        # A human_resumed event with the auto-approve marker references it.
        events = store.load_events(session_conn)
        hr = [e for e in events if e["type"] == EventType.HUMAN_RESUMED.value]
        assert hr, "no human_resumed event appended"
        latest = hr[-1]
        assert latest["material_id"] == approval_mid
        assert latest["payload"]["material_id"] == pending_mid
        decision = latest["payload"]["answer_or_decision"]
        assert decision.get("approved") is True
        assert decision.get("auto_approved_via_resume") is True

    session_row = store.load_session(setup["core_conn"], session_id)
    assert session_row is not None
    assert session_row["status"] == SessionStatus.ACTIVE.value
    assert int(session_row["iter_since_approval"]) == 0


def test_force_continue_leaves_ask_user_pending_alone(orchestrator_setup):
    """force_continue must NOT auto-answer a freeform ask_user pending.

    The pending row stays pending, no user_answer is written, status flips
    to active, counter resets, and the report flags 0 auto-approvals.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.AWAITING_HUMAN.value,
        iter_since_approval=4,
    )
    with setup["session_conn_factory"]() as session_conn:
        ask_mid = _seed_pending_ask_user(session_conn)

    with setup["session_conn_factory"]() as session_conn:
        report = force_continue(
            session_id,
            core_conn=setup["core_conn"],
            session_conn=session_conn,
        )

    assert report["auto_approved"] == 0
    assert report["previous_status"] == SessionStatus.AWAITING_HUMAN.value
    assert report["previous_iter"] == 4
    assert ask_mid in report["pending_remaining"]

    with setup["session_conn_factory"]() as session_conn:
        original = store.load_material(session_conn, ask_mid)
        assert original is not None
        assert original["pending"] is True

        # No user_answer should have been written for the ask_user material.
        answers = session_conn.execute(
            "SELECT COUNT(*) AS n FROM material WHERE type = ?",
            (MaterialType.USER_ANSWER.value,),
        ).fetchone()
        assert int(answers["n"]) == 0

    session_row = store.load_session(setup["core_conn"], session_id)
    assert session_row is not None
    assert session_row["status"] == SessionStatus.ACTIVE.value
    assert int(session_row["iter_since_approval"]) == 0


def test_force_continue_leaves_subject_approval_pending_alone(orchestrator_setup):
    """force_continue must NOT auto-approve a subject-based pending approval.

    A pending kind='approval' with subject='business_brief' is a real content
    gate; force_continue leaves it pending. Status flips to active, iter
    resets, the report flags 0 auto-approvals.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.AWAITING_HUMAN.value,
        iter_since_approval=2,
    )
    with setup["session_conn_factory"]() as session_conn:
        approval_mid = _seed_pending_approval(session_conn, subject="business_brief")

    with setup["session_conn_factory"]() as session_conn:
        report = force_continue(
            session_id,
            core_conn=setup["core_conn"],
            session_conn=session_conn,
        )

    assert report["auto_approved"] == 0
    assert approval_mid in report["pending_remaining"]

    with setup["session_conn_factory"]() as session_conn:
        original = store.load_material(session_conn, approval_mid)
        assert original is not None
        assert original["pending"] is True
        # No user_approval row should have been written.
        approvals = session_conn.execute(
            "SELECT COUNT(*) AS n FROM material WHERE type = ?",
            (MaterialType.USER_APPROVAL.value,),
        ).fetchone()
        assert int(approvals["n"]) == 0

    session_row = store.load_session(setup["core_conn"], session_id)
    assert session_row is not None
    assert session_row["status"] == SessionStatus.ACTIVE.value
    assert int(session_row["iter_since_approval"]) == 0


def test_force_continue_idempotent_on_active_session(orchestrator_setup):
    """force_continue on an active session with no pending materials is a no-op.

    Status stays active, no new events or materials, report.auto_approved=0,
    pending_remaining is empty.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]
    # Session is already active by default from create_session.
    with setup["session_conn_factory"]() as session_conn:
        events_before = store.load_events(session_conn)
        materials_before = session_conn.execute(
            "SELECT COUNT(*) AS n FROM material"
        ).fetchone()["n"]

    with setup["session_conn_factory"]() as session_conn:
        report = force_continue(
            session_id,
            core_conn=setup["core_conn"],
            session_conn=session_conn,
        )

    assert report["auto_approved"] == 0
    assert report["previous_status"] == SessionStatus.ACTIVE.value
    assert report["pending_remaining"] == []

    with setup["session_conn_factory"]() as session_conn:
        events_after = store.load_events(session_conn)
        materials_after = session_conn.execute(
            "SELECT COUNT(*) AS n FROM material"
        ).fetchone()["n"]
    # No new events appended (no continuation to approve), no new materials.
    assert events_after == events_before
    assert materials_after == materials_before

    session_row = store.load_session(setup["core_conn"], session_id)
    assert session_row is not None
    assert session_row["status"] == SessionStatus.ACTIVE.value


def test_force_continue_returns_correct_report(orchestrator_setup):
    """Smoke-test the report dict's exact keys and types."""
    setup = orchestrator_setup
    session_id = setup["session_id"]
    store.update_session_status(
        setup["core_conn"],
        session_id,
        SessionStatus.AWAITING_HUMAN.value,
        iter_since_approval=7,
    )
    with setup["session_conn_factory"]() as session_conn:
        cont_mid = _seed_pending_continuation(session_conn)
        ask_mid = _seed_pending_ask_user(session_conn)
        approval_mid = _seed_pending_approval(session_conn, subject="mockup")

    with setup["session_conn_factory"]() as session_conn:
        report = force_continue(
            session_id,
            core_conn=setup["core_conn"],
            session_conn=session_conn,
        )

    assert set(report.keys()) == {
        "auto_approved",
        "previous_status",
        "previous_iter",
        "pending_remaining",
    }
    assert isinstance(report["auto_approved"], int)
    assert isinstance(report["previous_status"], str)
    assert isinstance(report["previous_iter"], int)
    assert isinstance(report["pending_remaining"], list)
    assert all(isinstance(m, str) for m in report["pending_remaining"])

    assert report["auto_approved"] == 1
    assert report["previous_status"] == SessionStatus.AWAITING_HUMAN.value
    assert report["previous_iter"] == 7
    # The continuation_approval should be resolved, the ask_user and
    # subject-based approval should still be pending.
    assert cont_mid not in report["pending_remaining"]
    assert ask_mid in report["pending_remaining"]
    assert approval_mid in report["pending_remaining"]


def test_tool_failed_alarm_never_auto_resolved(orchestrator_setup):
    """Event-based alarms (tool_failed) must stay resolved=0 across runs.

    Raising a tool_failed alarm directly, then running a clean loop to
    completion, must leave the alarm unresolved — these reflect a past
    event, not a current condition the harness can re-check.
    """
    setup = orchestrator_setup
    session_id = setup["session_id"]

    with setup["session_conn_factory"]() as session_conn:
        from harness.services import alarms as _alarms

        target_id = _alarms.raise_tool_failed(
            session_conn,
            tool="write_file",
            args={"path": "x"},
            error_kind="sandbox_escape",
            error_message="denied",
            stage=Stage.BOOTSTRAP.value,
        )

    worker = MockWorker(scripted_responses=[Final(summary="normal exit")])
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
    )

    r = run_until_pause(session_id, config)
    assert r.status == SessionStatus.COMPLETED.value

    with setup["session_conn_factory"]() as session_conn:
        rows = store.load_alarms(session_conn)
    alarm = next(r for r in rows if r["id"] == target_id)
    assert alarm["resolved"] is False, (
        "tool_failed is event-based; must never be auto-resolved"
    )


# ---------------------------------------------------------------------------
# rewind_session — destructive truncation + session-row reset
# ---------------------------------------------------------------------------


def test_rewind_session_updates_status_and_iter(orchestrator_setup):
    """Rewind to an awaiting_human event flips status/stage/iter on the row."""
    from harness.services.orchestrator import rewind_session

    setup = orchestrator_setup
    session_id = setup["session_id"]

    # Drive a session that pauses on an ask_user.
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="ask_user", args={"question": "What is your name?"}),
        ToolCall(tool="ask_user", args={"question": "Second?"}),
        Final(summary="done"),
    ]
    worker = MockWorker(scripted_responses=scripted)
    workers_by_stage = {Stage.BOOTSTRAP.value: worker}
    config = _build_config(
        core_db_path=setup["core_db_path"],
        sessions_dir=setup["sessions_dir"],
        sandbox_root=setup["sandbox_root"],
        workers_by_stage=workers_by_stage,
    )

    # First pause on ask_user(1).
    r1 = run_until_pause(session_id, config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value

    # Capture the first awaiting_human event id.
    with setup["session_conn_factory"]() as session_conn:
        events = store.load_events(session_conn)
    first_awaiting = next(
        e for e in events if e["type"] == EventType.AWAITING_HUMAN.value
    )

    # Answer the first question; the worker proceeds and pauses again.
    with setup["session_conn_factory"]() as session_conn:
        pending = _last_awaiting_pending_material_id(session_conn)
        _simulate_human_answer(
            setup["core_conn"], session_conn, session_id,
            pending_material_id=pending, answer_text="Alice",
            stage=Stage.BOOTSTRAP.value,
        )
    r2 = run_until_pause(session_id, config)
    assert r2.status == SessionStatus.AWAITING_HUMAN.value

    # Force the iter counter high so we can prove rewind resets it.
    store.update_session_status(
        setup["core_conn"], session_id, SessionStatus.AWAITING_HUMAN.value,
        iter_since_approval=5,
    )

    # Rewind to the FIRST awaiting_human.
    with setup["session_conn_factory"]() as session_conn:
        report = rewind_session(
            session_id, first_awaiting["id"],
            core_conn=setup["core_conn"],
            session_conn=session_conn,
        )

    assert report["session_id"] == session_id
    assert report["target_event_id"] == first_awaiting["id"]
    assert report["repended_material_id"] == first_awaiting["payload"]["material_id"]

    session_row = store.load_session(setup["core_conn"], session_id)
    assert session_row is not None
    assert session_row["status"] == SessionStatus.AWAITING_HUMAN.value
    assert session_row["current_stage"] == first_awaiting["stage"]
    assert int(session_row["iter_since_approval"]) == 0


def test_rewind_session_then_answer_continues_normally(orchestrator_setup):
    """After a rewind, /answer with a different answer drives the loop afresh."""
    from harness.services.orchestrator import rewind_session

    setup = orchestrator_setup
    session_id = setup["session_id"]

    # MockWorker that emits 5 scripted responses across two paths. The script
    # is consumed in order regardless of rewinds (MockWorker is stateful),
    # so we use a script long enough for: turn 1 (ask_user A), turn 2 (Final
    # after first answer), then turn 3 (ask_user A again after rewind),
    # turn 4 (Final after second answer).
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(tool="ask_user", args={"question": "Pick"}),
        Final(summary="path-1"),
        ToolCall(tool="ask_user", args={"question": "Pick"}),
        Final(summary="path-2"),
    ]
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

    # Turn 1: pause on ask_user.
    run_until_pause(session_id, config)
    with setup["session_conn_factory"]() as session_conn:
        events_after_pause1 = store.load_events(session_conn)
        first_awaiting = next(
            e for e in events_after_pause1
            if e["type"] == EventType.AWAITING_HUMAN.value
        )
        pending_first = _last_awaiting_pending_material_id(session_conn)
        _simulate_human_answer(
            setup["core_conn"], session_conn, session_id,
            pending_material_id=pending_first, answer_text="first-answer",
            stage=Stage.BOOTSTRAP.value,
        )

    # Turn 2: completes with Final("path-1").
    r2 = run_until_pause(session_id, config)
    assert r2.status == SessionStatus.COMPLETED.value

    # Rewind back to the first awaiting_human.
    with setup["session_conn_factory"]() as session_conn:
        rewind_session(
            session_id, first_awaiting["id"],
            core_conn=setup["core_conn"],
            session_conn=session_conn,
        )

    # Answer the (re-pended) material differently. The next loop call
    # should produce the second Final via the second scripted ask_user pause.
    with setup["session_conn_factory"]() as session_conn:
        pending_again = _last_awaiting_pending_material_id(session_conn)
        # The re-pended material is the SAME id as the one we answered before.
        assert pending_again == pending_first
        _simulate_human_answer(
            setup["core_conn"], session_conn, session_id,
            pending_material_id=pending_again, answer_text="second-answer",
            stage=Stage.BOOTSTRAP.value,
        )

    # Next run consumes the next two scripted responses: ask_user + Final.
    r3 = run_until_pause(session_id, config)
    assert r3.status == SessionStatus.AWAITING_HUMAN.value
    # Answer the new pause to drive the Final.
    with setup["session_conn_factory"]() as session_conn:
        pending_new = _last_awaiting_pending_material_id(session_conn)
        _simulate_human_answer(
            setup["core_conn"], session_conn, session_id,
            pending_material_id=pending_new, answer_text="x",
            stage=Stage.BOOTSTRAP.value,
        )
    r4 = run_until_pause(session_id, config)
    assert r4.status == SessionStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# Bug-2 fixes: approving request_approval(subject='business_brief') persists
# the collected details as a new business_brief material.
# ---------------------------------------------------------------------------


_BRIEF_DETAILS_JIMS_HVAC = {
    "name": "Jim's HVAC",
    "industry": "Home service (HVAC)",
    "palette": {"primary": "#0055aa", "secondary": "#ffffff"},
}


def _drive_brief_approval(
    setup,
    *,
    details: dict,
    approved: bool,
) -> None:
    """Helper: run one turn that calls request_approval(business_brief), then
    simulate the human's approve/deny via _simulate_human_approval, then run
    the orchestrator again so the resume-fold step processes the response.
    """
    scripted: list[ToolCall | Final | Escalate] = [
        ToolCall(
            tool="request_approval",
            args={"subject": "business_brief", "details": details},
        ),
        # A second response so the loop has something to run after fold, but
        # the orchestrator will likely pause first on the awaiting_human or
        # complete depending on what's next; we use Final to terminate cleanly
        # in case the fold advances stage and the loop wants another turn.
        Final(summary="done"),
    ]
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

    # Turn 1: worker calls request_approval -> pause.
    r1 = run_until_pause(setup["session_id"], config)
    assert r1.status == SessionStatus.AWAITING_HUMAN.value
    assert r1.paused_reason == PAUSE_AWAITING_HUMAN

    # User approves/denies the brief.
    with setup["session_conn_factory"]() as session_conn:
        pending = _last_awaiting_pending_material_id(session_conn)
        _simulate_human_approval(
            setup["core_conn"],
            session_conn,
            setup["session_id"],
            pending_material_id=pending,
            approved=approved,
            subject="business_brief",
            stage=Stage.BOOTSTRAP.value,
        )

    # Re-enter the loop so the resume-fold step runs.
    run_until_pause(setup["session_id"], config)


def test_brief_approval_persists_business_brief_material(orchestrator_setup):
    """Approving request_approval(subject='business_brief') persists details
    as a new business_brief material so render_mockup sees the user's brief."""
    setup = orchestrator_setup

    # Pre-condition: no business_brief material exists yet (session was
    # created empty per Fix 1).
    with setup["session_conn_factory"]() as session_conn:
        assert store.latest_material_by_type(
            session_conn, MaterialType.BUSINESS_BRIEF.value
        ) is None

    _drive_brief_approval(
        setup, details=_BRIEF_DETAILS_JIMS_HVAC, approved=True
    )

    with setup["session_conn_factory"]() as session_conn:
        latest = store.latest_material_by_type(
            session_conn, MaterialType.BUSINESS_BRIEF.value
        )
        assert latest is not None
        content = latest["content"]
        assert content["name"] == "Jim's HVAC"
        assert content["industry"] == "Home service (HVAC)"
        assert content["palette"] == {
            "primary": "#0055aa",
            "secondary": "#ffffff",
        }
        assert latest["pending"] is False
        assert latest["direction"] == Direction.OUT.value

        # business_brief_confirmed checkpoint also passes (existing behavior).
        ckpts = store.load_checkpoints(
            session_conn, name=CheckpointName.BUSINESS_BRIEF_CONFIRMED.value
        )
        assert ckpts, "business_brief_confirmed checkpoint not recorded"
        assert ckpts[-1]["status"] == CheckpointStatus.PASS.value


def test_brief_approval_with_empty_details_does_not_crash(orchestrator_setup):
    """Approving with details={} skips the persist (no crash) and still
    evaluates the checkpoint."""
    setup = orchestrator_setup

    _drive_brief_approval(setup, details={}, approved=True)

    with setup["session_conn_factory"]() as session_conn:
        # No business_brief material persisted (empty details is a no-op).
        latest = store.latest_material_by_type(
            session_conn, MaterialType.BUSINESS_BRIEF.value
        )
        assert latest is None

        # The checkpoint still evaluates. It may fail (no brief on file), but
        # the row must exist — that's the existing behavior we preserve.
        ckpts = store.load_checkpoints(
            session_conn, name=CheckpointName.BUSINESS_BRIEF_CONFIRMED.value
        )
        assert ckpts, "business_brief_confirmed checkpoint not recorded"


def test_brief_denial_does_not_persist_brief(orchestrator_setup):
    """Denying the brief approval must NOT persist a business_brief material."""
    setup = orchestrator_setup

    _drive_brief_approval(
        setup, details=_BRIEF_DETAILS_JIMS_HVAC, approved=False
    )

    with setup["session_conn_factory"]() as session_conn:
        latest = store.latest_material_by_type(
            session_conn, MaterialType.BUSINESS_BRIEF.value
        )
        assert latest is None
