"""The orchestrator loop: `run_until_pause(session_id, config)`.

Topmost service-layer module. May import every other module under
`harness.services.*` plus `harness.models.*`. MUST NOT import from
`harness.domain`, `harness.api`, or `harness.templates`.

Responsibilities:
  - Drive the per-turn loop: pre-turn guardrails -> build WorkerContext ->
    call worker.act -> dispatch tool -> run post-hooks (after write_file) ->
    evaluate checkpoints -> persist everything.
  - Detect human resumes (most recent event is `human_resumed`) and run
    approval-driven checkpoint evaluations + stage advancement BEFORE the
    next turn's guardrail checks.
  - Fail-as-data: tool failures, schema violations, worker exceptions, and
    post-hook errors become alarm rows + recorded events, never exceptions
    propagated out of `run_until_pause`.

This module performs no HTTP, no LLM calls, and is synchronous/single-process.
State lives entirely in SQLite, so the function is safely re-callable after
a crash; replay reads the event log and resumes deterministically.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harness.models.enums import (
    CheckpointName,
    Direction,
    EventType,
    MaterialType,
    SessionStatus,
    Stage,
)
from harness.models.envelope import Escalate, Final, Message, ToolCall, WorkerContext
from harness.services import (
    alarms,
    checkpoints,
    guardrails,
    post_hooks,
    store,
)
from harness.services.tools import ToolContext, ToolResult, dispatch
from harness.services.worker import Worker


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorConfig:
    """Injected configuration for one run.

    The orchestrator never knows whether a worker is real (LLMWorker) or a
    MockWorker — both satisfy `Worker`. `sandbox_root_for` returns the per-
    session sandbox path; the orchestrator creates the directory if missing.
    """

    worker_for_stage: Callable[[str], Worker]
    system_prompt: str
    allow_list: list[str]
    sandbox_root_for: Callable[[str], Path]
    core_db_path: Path
    sessions_dir: Path
    turn_cap: int = 10
    spend_cap_usd: float = 1.0


@dataclass
class RunResult:
    """Outcome of one `run_until_pause` invocation.

    `paused_reason` is None on terminal exits (status in {'completed',
    'failed'}). `turns_executed` counts only turns taken in THIS call —
    not lifetime turns across resumes.
    """

    session_id: str
    status: str
    current_stage: str
    terminal: bool
    paused_reason: str | None
    turns_executed: int


_PAUSE_SPEND_CAP = "spend_cap"
_PAUSE_TURN_CAP = "turn_cap"
_PAUSE_AWAITING_HUMAN = "awaiting_human"
_PAUSE_OUTPUT_SCHEMA = "output_schema_violation"
_PAUSE_TOOL_FAILED = "tool_failed"


def run_until_pause(session_id: str, config: OrchestratorConfig) -> RunResult:
    """Drive the loop until pause, terminal, or guardrail trip.

    Re-callable after a crash: state survives in SQLite; the next invocation
    rebuilds messages from the event log and continues from where it left
    off. No-op if the session is already in a terminal/awaiting state.
    """
    core_conn = store.core_connection(Path(config.core_db_path))
    session_conn = store.session_connection(Path(config.sessions_dir), session_id)
    try:
        return _run_loop(session_id, config, core_conn, session_conn)
    finally:
        session_conn.close()
        core_conn.close()


def _run_loop(
    session_id: str,
    config: OrchestratorConfig,
    core_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
) -> RunResult:
    turns_executed = 0

    # No-op for sessions already at rest. Anything other than 'active' means
    # a prior run hit a pause/terminal and the harness has not been told to
    # resume yet (resume flips status back to 'active').
    session = store.load_session(core_conn, session_id)
    if session is None:
        raise RuntimeError(f"session {session_id!r} not found in core DB")
    if session["status"] != SessionStatus.ACTIVE.value:
        return RunResult(
            session_id=session_id,
            status=session["status"],
            current_stage=session["current_stage"],
            terminal=_is_terminal_status(session["status"]),
            paused_reason=None,
            turns_executed=0,
        )

    while True:
        # Step 12 (executed BEFORE guardrails each turn): if the most recent
        # event is a human_resumed, fold the approval into checkpoint
        # evaluations + possible stage advancement BEFORE we run guardrails
        # or call the worker. Reload the session row whenever we mutate it.
        _consume_resume_if_present(core_conn, session_conn, session_id)
        session = store.load_session(core_conn, session_id)
        if session is None:
            raise RuntimeError(f"session {session_id!r} vanished mid-loop")
        stage = session["current_stage"]

        # Step 1 — pre-turn guardrails.
        spent = store.recent_spend_today_usd(core_conn)
        if guardrails.check_spend_cap_today(spent, config.spend_cap_usd):
            alarms.raise_spend_cap_reached(
                session_conn,
                spent_usd=spent,
                cap_usd=config.spend_cap_usd,
                stage=stage,
            )
            # Persist a pending continuation_approval material so the UI can
            # render an "approve / stop" affordance. Caveat: the spend cap is
            # rolling-24h, so approving here keeps tripping the cap until the
            # window rolls — the question text says so explicitly.
            store.persist_material(
                session_conn,
                direction=Direction.OUT.value,
                stage=stage,
                type=MaterialType.PENDING_QUESTION.value,
                content={
                    "kind": "continuation_approval",
                    "question": (
                        f"The agent has spent ${spent:.4f} today and hit the"
                        f" daily cap (${config.spend_cap_usd:.4f}). Approving"
                        " continues anyway, but the cap is rolling-24h so the"
                        " session will keep tripping until the window rolls."
                        " Continue, or stop?"
                    ),
                    "spent_usd": spent,
                    "cap_usd": config.spend_cap_usd,
                },
                pending=True,
            )
            store.update_session_status(
                core_conn, session_id, SessionStatus.AWAITING_HUMAN.value
            )
            return RunResult(
                session_id=session_id,
                status=SessionStatus.AWAITING_HUMAN.value,
                current_stage=stage,
                terminal=False,
                paused_reason=_PAUSE_SPEND_CAP,
                turns_executed=turns_executed,
            )

        iter_count = int(session["iter_since_approval"])
        if guardrails.check_turn_cap(iter_count, config.turn_cap):
            last_checkpoint = _last_checkpoint_name(session_conn)
            alarms.raise_iteration_limit_reached(
                session_conn,
                iter_count=iter_count,
                last_checkpoint=last_checkpoint,
                stage=stage,
            )
            # Persist a pending continuation_approval material so the UI can
            # render an "approve / stop" affordance.
            store.persist_material(
                session_conn,
                direction=Direction.OUT.value,
                stage=stage,
                type=MaterialType.PENDING_QUESTION.value,
                content={
                    "kind": "continuation_approval",
                    "question": (
                        f"The agent has run {iter_count} autonomous iterations"
                        " since your last input and hit the safety cap."
                        " Approve another 10 iterations, or stop?"
                    ),
                    "iter_count": iter_count,
                },
                pending=True,
            )
            store.update_session_status(
                core_conn, session_id, SessionStatus.AWAITING_HUMAN.value
            )
            return RunResult(
                session_id=session_id,
                status=SessionStatus.AWAITING_HUMAN.value,
                current_stage=stage,
                terminal=False,
                paused_reason=_PAUSE_TURN_CAP,
                turns_executed=turns_executed,
            )

        # Step 2 — build WorkerContext from the persisted event log.
        sandbox_path = Path(config.sandbox_root_for(session_id))
        sandbox_path.mkdir(parents=True, exist_ok=True)
        ctx = _build_context(
            session_conn=session_conn,
            session_id=session_id,
            stage=stage,
            system_prompt=config.system_prompt,
            sandbox_path=sandbox_path,
        )

        # Step 3 — record worker_input.
        tokens_estimate = sum(len(m.content) for m in ctx.messages) // 4
        store.append_event(
            session_conn,
            type=EventType.WORKER_INPUT.value,
            stage=stage,
            payload={
                "model": "unknown_at_this_layer",
                "messages_count": len(ctx.messages),
                "tokens_estimate": tokens_estimate,
            },
        )

        # Step 4 — call worker. Defensive: any exception becomes an alarm.
        worker = config.worker_for_stage(stage)
        try:
            response = worker.act(ctx)
        except Exception as exc:
            alarms.raise_tool_failed(
                session_conn,
                tool="worker.act",
                args={},
                error_kind="worker_exception",
                error_message=str(exc),
                stage=stage,
            )
            store.update_session_status(
                core_conn, session_id, SessionStatus.AWAITING_HUMAN.value
            )
            return RunResult(
                session_id=session_id,
                status=SessionStatus.AWAITING_HUMAN.value,
                current_stage=stage,
                terminal=False,
                paused_reason=_PAUSE_TOOL_FAILED,
                turns_executed=turns_executed,
            )

        # Step 5 — envelope-handling defensive check. LLMWorker (Step 8) will
        # parse JSON itself and surface its own output_schema_violation; this
        # path catches non-typed values from a misbehaving worker subclass.
        if not isinstance(response, (ToolCall, Final, Escalate)):
            preview = repr(response)[:200]
            alarms.raise_output_schema_violation(
                session_conn,
                parse_error="worker returned non-envelope object",
                repair_attempt=0,
                raw_text_preview=preview,
                stage=stage,
            )
            store.update_session_status(
                core_conn, session_id, SessionStatus.AWAITING_HUMAN.value
            )
            return RunResult(
                session_id=session_id,
                status=SessionStatus.AWAITING_HUMAN.value,
                current_stage=stage,
                terminal=False,
                paused_reason=_PAUSE_OUTPUT_SCHEMA,
                turns_executed=turns_executed,
            )

        turns_executed += 1

        # Step 6 — record worker_output.
        store.append_event(
            session_conn,
            type=EventType.WORKER_OUTPUT.value,
            stage=stage,
            payload={
                "envelope": response.model_dump(),
                "model": "unknown_at_this_layer",
            },
        )

        # Step 7 — branch on response type.
        if isinstance(response, Final):
            store.update_session_status(
                core_conn,
                session_id,
                SessionStatus.COMPLETED.value,
                current_stage=Stage.DONE.value,
            )
            return RunResult(
                session_id=session_id,
                status=SessionStatus.COMPLETED.value,
                current_stage=Stage.DONE.value,
                terminal=True,
                paused_reason=None,
                turns_executed=turns_executed,
            )

        if isinstance(response, Escalate):
            alarms.raise_tool_failed(
                session_conn,
                tool="escalate",
                args={},
                error_kind="escalated_by_worker",
                error_message=response.reason,
                stage=stage,
            )
            store.update_session_status(
                core_conn, session_id, SessionStatus.AWAITING_HUMAN.value
            )
            return RunResult(
                session_id=session_id,
                status=SessionStatus.AWAITING_HUMAN.value,
                current_stage=stage,
                terminal=False,
                paused_reason=_PAUSE_AWAITING_HUMAN,
                turns_executed=turns_executed,
            )

        # ToolCall path — dispatch + post-hooks + checkpoints.
        tool_call: ToolCall = response
        tool_ctx = ToolContext(
            session_conn=session_conn,
            sandbox_path=sandbox_path,
            stage=stage,
            allow_list=list(config.allow_list),
        )
        store.append_event(
            session_conn,
            type=EventType.TOOL_CALL.value,
            stage=stage,
            payload={
                "tool": tool_call.tool,
                "args": tool_call.args,
                "allowed": guardrails.is_tool_allowed(
                    tool_call.tool, config.allow_list
                ),
            },
        )

        tool_result: ToolResult = dispatch(tool_call.tool, tool_call.args, tool_ctx)

        # Record tool_result. When ok=False, the dispatcher already raised
        # a tool_failed alarm; this event carries the error data for the
        # timeline.
        store.append_event(
            session_conn,
            type=EventType.TOOL_RESULT.value,
            stage=stage,
            payload={
                "tool": tool_call.tool,
                "ok": tool_result.ok,
                "result_or_error": (
                    tool_result.result if tool_result.ok else tool_result.error
                ),
            },
        )

        # Step 8 — post-hooks + write_file checkpoints.
        if tool_result.ok and tool_call.tool == "write_file":
            report = post_hooks.run(sandbox_path)
            store.append_event(
                session_conn,
                type=EventType.POST_HOOK_RUN.value,
                stage=stage,
                payload={
                    "validate_ok": report.validate_ok,
                    "seo_regenerated": report.seo_regenerated,
                    "git_commit_sha": report.git_commit_sha,
                },
            )
            validation_material_id = store.persist_material(
                session_conn,
                direction=Direction.OUT.value,
                stage=stage,
                type=MaterialType.VALIDATION_RESULT.value,
                content={
                    "html": report.html_reports,
                    "css": report.css_reports,
                    "seo": report.seo_report,
                },
            )
            site_valid = checkpoints.evaluate_site_valid(
                html_reports=report.html_reports,
                css_reports=report.css_reports,
            )
            _persist_and_record_checkpoint(
                session_conn,
                site_valid,
                stage=stage,
                material_id=validation_material_id,
            )
            seo_ok = checkpoints.evaluate_seo_artifacts_present(
                seo_report=report.seo_report
            )
            _persist_and_record_checkpoint(
                session_conn,
                seo_ok,
                stage=stage,
                material_id=validation_material_id,
            )

        # Step 9 — render_mockup checkpoint.
        if tool_result.ok and tool_call.tool == "render_mockup":
            material_id = (tool_result.result or {}).get("material_id")
            mockup_material = (
                store.load_material(session_conn, material_id)
                if isinstance(material_id, str)
                else None
            )
            declared_sections = _declared_sections_from_args(tool_call.args)
            mockup_check = checkpoints.evaluate_mockup_renders(
                mockup_material=mockup_material,
                declared_sections=declared_sections,
            )
            _persist_and_record_checkpoint(
                session_conn,
                mockup_check,
                stage=stage,
                material_id=material_id if isinstance(material_id, str) else None,
            )

        # Step 10 — HITL pause sentinel (ask_user / request_approval).
        if tool_result.pause_reason == _PAUSE_AWAITING_HUMAN:
            material_id = (tool_result.result or {}).get("material_id")
            store.append_event(
                session_conn,
                type=EventType.AWAITING_HUMAN.value,
                stage=stage,
                payload={"material_id": material_id, "reason": tool_call.tool},
                material_id=material_id if isinstance(material_id, str) else None,
            )
            store.update_session_status(
                core_conn,
                session_id,
                SessionStatus.AWAITING_HUMAN.value,
                iter_since_approval=iter_count + 1,
            )
            return RunResult(
                session_id=session_id,
                status=SessionStatus.AWAITING_HUMAN.value,
                current_stage=stage,
                terminal=False,
                paused_reason=_PAUSE_AWAITING_HUMAN,
                turns_executed=turns_executed,
            )

        # Step 11 — bump turn counter and loop.
        store.update_session_status(
            core_conn,
            session_id,
            SessionStatus.ACTIVE.value,
            iter_since_approval=iter_count + 1,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_terminal_status(status: str) -> bool:
    return status in {SessionStatus.COMPLETED.value, SessionStatus.FAILED.value}


def _persist_and_record_checkpoint(
    session_conn: sqlite3.Connection,
    result: checkpoints.CheckpointResult,
    *,
    stage: str,
    material_id: str | None,
) -> str:
    """Persist a CheckpointResult and append its checkpoint_result event."""
    cid = store.persist_checkpoint(
        session_conn,
        name=result.name,
        stage=stage,
        status=result.status,
        criteria_results=result.criteria_results,
        material_id=material_id,
    )
    store.append_event(
        session_conn,
        type=EventType.CHECKPOINT_RESULT.value,
        stage=stage,
        payload={
            "name": result.name,
            "status": result.status,
            "criteria_results": result.criteria_results,
        },
        checkpoint_id=cid,
    )
    return cid


def _declared_sections_from_args(args: dict) -> list[str]:
    """Pull section names from a render_mockup args dict; tolerate bad input."""
    layout = args.get("layout_spec") if isinstance(args, dict) else None
    if not isinstance(layout, dict):
        return []
    raw = layout.get("sections")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for s in raw:
        if isinstance(s, dict) and isinstance(s.get("name"), str):
            names.append(s["name"])
    return names


def _last_checkpoint_name(session_conn: sqlite3.Connection) -> str | None:
    rows = store.load_checkpoints(session_conn)
    if not rows:
        return None
    return rows[-1]["name"]


def _last_alarm(session_conn: sqlite3.Connection) -> dict | None:
    rows = store.load_alarms(session_conn)
    if not rows:
        return None
    return rows[-1]


def _latest_brief_content(session_conn: sqlite3.Connection) -> dict | None:
    """Return the most recent business_brief material content dict, if any."""
    row = session_conn.execute(
        "SELECT id, direction, stage, type, content, pending, created_at"
        " FROM material WHERE type = ? ORDER BY id DESC LIMIT 1",
        (MaterialType.BUSINESS_BRIEF.value,),
    ).fetchone()
    if row is None:
        return None
    content = json.loads(row["content"])
    return content if isinstance(content, dict) else None


def _latest_brief_material(session_conn: sqlite3.Connection) -> dict | None:
    row = session_conn.execute(
        "SELECT id, direction, stage, type, content, pending, created_at"
        " FROM material WHERE type = ? ORDER BY id DESC LIMIT 1",
        (MaterialType.BUSINESS_BRIEF.value,),
    ).fetchone()
    if row is None:
        return None
    return store.load_material(session_conn, row["id"])


def _build_context(
    *,
    session_conn: sqlite3.Connection,
    session_id: str,
    stage: str,
    system_prompt: str,
    sandbox_path: Path,
) -> WorkerContext:
    """Assemble the WorkerContext for the next worker.act() call.

    Messages are reconstructed from the event log in chronological order
    (events.id is UUID7, so order-by-id is order-of-insertion). Only
    worker_output, tool_result, and human_resumed events contribute messages
    — tool_call events are intentionally skipped so the role alternation the
    LLM expects (assistant -> user) is preserved.
    """
    events = store.load_events(session_conn)
    brief = _latest_brief_content(session_conn)

    addendum = ""
    if brief is not None:
        addendum = "\n\n## Business Brief\n" + json.dumps(brief)

    messages: list[Message] = [
        Message(role="system", content=system_prompt + addendum)
    ]

    for e in events:
        etype = e["type"]
        payload = e["payload"]
        if etype == EventType.WORKER_OUTPUT.value:
            envelope = payload.get("envelope") if isinstance(payload, dict) else None
            messages.append(
                Message(role="assistant", content=json.dumps(envelope))
            )
        elif etype == EventType.TOOL_RESULT.value:
            tool = payload.get("tool") if isinstance(payload, dict) else None
            result = (
                payload.get("result_or_error") if isinstance(payload, dict) else None
            )
            messages.append(
                Message(
                    role="user",
                    content=f"Tool {tool}: {json.dumps(result)}",
                )
            )
        elif etype == EventType.HUMAN_RESUMED.value:
            answer = (
                payload.get("answer_or_decision")
                if isinstance(payload, dict)
                else None
            )
            messages.append(
                Message(role="user", content=f"User: {json.dumps(answer)}")
            )

    last_checkpoint = _last_checkpoint_name(session_conn)
    last_alarm = _last_alarm(session_conn)
    state: dict = {
        "last_checkpoint": last_checkpoint,
        "last_alarm": last_alarm,
        "brief": brief,
        "sandbox_path": str(sandbox_path),
    }

    worker_output_count = sum(
        1 for e in events if e["type"] == EventType.WORKER_OUTPUT.value
    )
    turn = worker_output_count + 1

    return WorkerContext(
        session_id=session_id,
        turn=turn,
        stage=stage,
        system_prompt=system_prompt + addendum,
        messages=messages,
        tool_schemas=[],
        state=state,
    )


def _consume_resume_if_present(
    core_conn: sqlite3.Connection,
    session_conn: sqlite3.Connection,
    session_id: str,
) -> None:
    """If the most recent event is a human_resumed, fold its approval into
    checkpoint evaluations and possible stage advancement.

    No-op when the latest event is anything else (or there are no events
    yet). Idempotent: once the corresponding checkpoint has been written
    and the session has moved on, calling this again is harmless because
    the next loop iteration adds new events on top.

    Approval rules:
      - user_approval with subject='business_brief' and approved=True ->
        business_brief_confirmed checkpoint; advance bootstrap -> mockup.
      - user_approval with subject='mockup' and approved=True ->
        mockup_approved checkpoint; advance mockup -> build.
      - Any approval (approved=True) resets iter_since_approval to 0.
    """
    events = store.load_events(session_conn)
    if not events:
        return
    latest = events[-1]
    if latest["type"] != EventType.HUMAN_RESUMED.value:
        return

    session = store.load_session(core_conn, session_id)
    if session is None:
        return
    stage = session["current_stage"]

    payload = latest["payload"]
    if not isinstance(payload, dict):
        return
    material_id = payload.get("material_id")
    if not isinstance(material_id, str):
        return

    answer_material = store.load_material(session_conn, material_id)
    content = answer_material.get("content") if answer_material else None
    content_dict = content if isinstance(content, dict) else None

    # Denial of a continuation_approval keeps the session paused — the user
    # said "stop" so we must not reset the counter or flip back to active.
    # The /answer route already flipped status to active before calling us;
    # revert that here so the next run_until_pause call no-ops.
    if (
        content_dict is not None
        and content_dict.get("kind") == "continuation_approval"
        and content_dict.get("approved") is False
    ):
        store.update_session_status(
            core_conn,
            session_id,
            SessionStatus.AWAITING_HUMAN.value,
        )
        return

    # Issue 1 fix: ANY fresh human_resumed event means a human just intervened,
    # so the "autonomous iterations between human inputs" counter must reset.
    # We do this BEFORE the approval-specific branching below so even plain
    # ask_user answers (which need no checkpoint) zero the counter.
    if int(session["iter_since_approval"]) != 0:
        store.update_session_status(
            core_conn,
            session_id,
            session["status"],
            iter_since_approval=0,
        )
        # Reload so downstream stage-advance writes see the zeroed counter
        # and don't accidentally clobber it with a stale value.
        session = store.load_session(core_conn, session_id)
        if session is None:
            return

    if answer_material is None or content_dict is None:
        return

    # We only act on approval-shaped resumes; plain ask_user answers feed
    # back into the worker via message history and need no checkpoint here.
    is_approval = (
        answer_material.get("type") == MaterialType.USER_APPROVAL.value
        or content_dict.get("kind") == "approval"
    )
    if not is_approval:
        return

    approved = bool(content_dict.get("approved"))
    subject = content_dict.get("subject")

    # If this resume has already been folded in (i.e. a checkpoint of the
    # matching name was written after the human_resumed event), skip.
    if _checkpoint_after(session_conn, latest["id"], subject):
        return

    # Note: iter_since_approval was already reset to 0 above for ANY
    # human_resumed event; the branches below only handle approval-specific
    # checkpoint evaluation and stage advancement.
    if subject == "business_brief":
        brief_material = _latest_brief_material(session_conn)
        result = checkpoints.evaluate_business_brief_confirmed(
            brief_material=brief_material,
            approval_material=answer_material,
        )
        _persist_and_record_checkpoint(
            session_conn,
            result,
            stage=stage,
            material_id=material_id,
        )
        new_stage = (
            Stage.MOCKUP.value
            if (approved and stage == Stage.BOOTSTRAP.value)
            else stage
        )
        store.update_session_status(
            core_conn,
            session_id,
            SessionStatus.ACTIVE.value,
            current_stage=new_stage,
            iter_since_approval=0,
        )
    elif subject == "mockup":
        result = checkpoints.evaluate_mockup_approved(
            approval_material=answer_material,
        )
        _persist_and_record_checkpoint(
            session_conn,
            result,
            stage=stage,
            material_id=material_id,
        )
        new_stage = (
            Stage.BUILD.value
            if (approved and stage == Stage.MOCKUP.value)
            else stage
        )
        store.update_session_status(
            core_conn,
            session_id,
            SessionStatus.ACTIVE.value,
            current_stage=new_stage,
            iter_since_approval=0,
        )
    else:
        # Unknown approval subject (e.g. continuation_approval): no stage
        # advance, no checkpoint — but flip to active so the loop resumes.
        # The counter was already zeroed above.
        store.update_session_status(
            core_conn,
            session_id,
            SessionStatus.ACTIVE.value,
            iter_since_approval=0,
        )


def _checkpoint_after(
    session_conn: sqlite3.Connection,
    event_id: str,
    subject: object,
) -> bool:
    """Idempotency guard for resume folding.

    Returns True if a checkpoint matching `subject` was persisted after the
    given human_resumed event id. Used to short-circuit a second fold if the
    loop ran a turn and is now being re-entered for the same resume payload.
    """
    name_by_subject = {
        "business_brief": CheckpointName.BUSINESS_BRIEF_CONFIRMED.value,
        "mockup": CheckpointName.MOCKUP_APPROVED.value,
    }
    target_name = name_by_subject.get(subject if isinstance(subject, str) else "")
    if target_name is None:
        return False
    rows = store.load_checkpoints(session_conn, name=target_name)
    for row in rows:
        if str(row["id"]) > str(event_id):
            return True
    return False


# Re-export the canonical pause-reason strings so callers (FastAPI handlers,
# tests) can build assertions without hard-coding string literals.
PAUSE_SPEND_CAP = _PAUSE_SPEND_CAP
PAUSE_TURN_CAP = _PAUSE_TURN_CAP
PAUSE_AWAITING_HUMAN = _PAUSE_AWAITING_HUMAN
PAUSE_OUTPUT_SCHEMA = _PAUSE_OUTPUT_SCHEMA
PAUSE_TOOL_FAILED = _PAUSE_TOOL_FAILED


__all__ = [
    "OrchestratorConfig",
    "RunResult",
    "run_until_pause",
    "PAUSE_SPEND_CAP",
    "PAUSE_TURN_CAP",
    "PAUSE_AWAITING_HUMAN",
    "PAUSE_OUTPUT_SCHEMA",
    "PAUSE_TOOL_FAILED",
]
