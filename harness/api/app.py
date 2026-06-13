"""FastAPI app — the 5 JSON routes documented in docs/http-api.md plus the
server-rendered web UI routes (Step 10).

Layer rule: thin handlers. Validation + serialization here; everything else
is delegated to `harness.services.*` (via `harness.domain.website_builder` for
orchestration plumbing). HTML route handlers read from `harness.services.store`
and pass data into Jinja2 templates rooted at `harness/templates/`.

All routes are sync (`def`, not `async def`). POST /sessions/{id}/resume and
POST /sessions/{id}/answer both call `orchestrator.run_until_pause` inline
and block for its duration — no background tasks.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from harness.api.dependencies import (
    AppContext,
    build_default_app_context,
    get_app_context,
    open_connections,
)
from harness.domain.website_builder import (
    RESTAURANT_SEED_BRIEF,
    make_orchestrator_config,
)
from harness.models.enums import (
    Direction,
    EventType,
    MaterialType,
    SessionStatus,
    Stage,
)
from harness.services import store
from harness.services.orchestrator import RunResult, run_until_pause


# Templates env shared by all HTML routes.  Resolved relative to this module
# so the app works regardless of the process cwd.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """POST /sessions accepts an empty body for v1."""

    # Allow but ignore unknown fields so clients sending `{}` or `{"foo": 1}`
    # don't get a 422 — the contract is "no per-session config in v1".
    model_config = ConfigDict(extra="ignore")


class AnswerRequest(BaseModel):
    """POST /sessions/{id}/answer body — see docs/http-api.md."""

    material_id: str = Field(..., min_length=1)
    answer_text: str | None = None
    approved: bool | None = None
    notes: str | None = None

    model_config = ConfigDict(extra="ignore")


class ResumeRequest(BaseModel):
    """POST /sessions/{id}/resume body — empty object per spec."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Error helpers — every non-2xx body has shape {"error": <code>, "detail": {...}}
# ---------------------------------------------------------------------------


def _error_response(
    *,
    status_code: int,
    error: str,
    detail: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "detail": detail or {}},
    )


class _ApiError(HTTPException):
    """HTTPException subclass that carries our standard {error, detail} body.

    We catch this in an exception handler so FastAPI returns the body in the
    canonical shape rather than its default `{"detail": "..."}`.
    """

    def __init__(
        self,
        *,
        status_code: int,
        error: str,
        detail: dict | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail or {})
        self.error_code = error


def _not_found(error: str, detail: dict | None = None) -> _ApiError:
    return _ApiError(
        status_code=status.HTTP_404_NOT_FOUND, error=error, detail=detail
    )


def _bad_request(error: str, detail: dict | None = None) -> _ApiError:
    return _ApiError(
        status_code=status.HTTP_400_BAD_REQUEST, error=error, detail=detail
    )


# ---------------------------------------------------------------------------
# Serializers — turn store dicts into JSON-clean output bodies
# ---------------------------------------------------------------------------


def _serialize_session(row: dict) -> dict:
    return {
        "id": row["id"],
        "status": row["status"],
        "current_stage": row["current_stage"],
        "iter_since_approval": int(row["iter_since_approval"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _serialize_event(row: dict) -> dict:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "type": row["type"],
        "stage": row["stage"],
        "payload": row["payload"],
        "material_id": row["material_id"],
        "checkpoint_id": row["checkpoint_id"],
        "alarm_id": row["alarm_id"],
    }


def _serialize_checkpoint(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "stage": row["stage"],
        "status": row["status"],
        "criteria_results": row["criteria_results"],
        "material_id": row["material_id"],
        "created_at": row["created_at"],
    }


def _serialize_alarm(row: dict) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "severity": row["severity"],
        "context": row["context"],
        "recommended_action": row["recommended_action"],
        "stage": row["stage"],
        "triggered_by_event_id": row["triggered_by_event_id"],
        "resolved": bool(row["resolved"]),
        "created_at": row["created_at"],
    }


def _serialize_material(row: dict) -> dict:
    return {
        "id": row["id"],
        "direction": row["direction"],
        "stage": row["stage"],
        "type": row["type"],
        "content": row["content"],
        "pending": bool(row["pending"]),
        "created_at": row["created_at"],
    }


def _serialize_run_result(result: RunResult) -> dict:
    return {
        "session_id": result.session_id,
        "status": result.status,
        "current_stage": result.current_stage,
        "terminal": result.terminal,
        "paused_reason": result.paused_reason,
    }


# ---------------------------------------------------------------------------
# Route handlers — see docs/http-api.md for the contract
# ---------------------------------------------------------------------------


_EVENTS_CAP = 500
# Max events shown in the web UI events panel (the JSON API stays at 500).
_UI_EVENTS_CAP = 50


def _models_from_env() -> dict:
    """Read the four MODEL_* env vars for display on the session detail page.

    Returns blank strings (not the domain bundle's defaults) for unset vars so
    the UI shows what the operator actually configured. Reading env per
    request is cheap and means a `.env` edit is picked up on next page load.
    """
    return {
        "chat_primary": os.environ.get("MODEL_CHAT", ""),
        "chat_fallback": os.environ.get("MODEL_CHAT_FALLBACK", ""),
        "code_primary": os.environ.get("MODEL_CODE", ""),
        "code_fallback": os.environ.get("MODEL_CODE_FALLBACK", ""),
    }


def _summarize_event_payload(event: dict) -> str:
    """Render a short, human-readable summary of an event row for the UI.

    Keeps the template logic-free. The full payload is shown inside a
    <details> block; this is the visible <summary>.
    """
    etype = event.get("type", "?")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return etype
    if etype == "worker_input":
        return f"model={payload.get('model', '?')} msgs={payload.get('messages_count', 0)}"
    if etype == "worker_output":
        wt = payload.get("type")
        if wt == "tool_call":
            return f"tool_call -> {payload.get('tool', '?')}"
        if wt == "final":
            return f"final: {str(payload.get('summary', ''))[:60]}"
        if wt == "escalate":
            return f"escalate: {str(payload.get('reason', ''))[:60]}"
        return wt or etype
    if etype == "model_swapped":
        return f"swap {payload.get('from', '?')} -> {payload.get('to', '?')}"
    if etype == "tool_call":
        return f"{payload.get('tool', '?')} (allowed={payload.get('allowed', '?')})"
    if etype == "tool_result":
        return f"{payload.get('tool', '?')} ok={payload.get('ok', '?')}"
    if etype == "post_hook_run":
        return f"validate_ok={payload.get('validate_ok', '?')} seo={payload.get('seo_regenerated', '?')}"
    if etype == "checkpoint_result":
        return f"{payload.get('name', '?')} -> {payload.get('status', '?')}"
    if etype == "alarm_raised":
        return f"{payload.get('type', '?')} ({payload.get('severity', '?')})"
    if etype == "awaiting_human":
        return f"reason={payload.get('reason', '?')}"
    if etype == "human_resumed":
        return f"material_id={payload.get('material_id', '?')[:8]}"
    return etype


def _create_app_routes(app: FastAPI) -> None:
    @app.post("/sessions")
    def create_session(
        body: CreateSessionRequest,
        ctx: AppContext = Depends(get_app_context),
    ) -> JSONResponse:
        # Empty-body request validated by pydantic; create row + seed brief.
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            session_id = store.create_session(
                conns.core_conn, current_stage=Stage.BOOTSTRAP.value
            )

        # Open a per-session connection to seed the restaurant brief so the
        # worker has a Business Brief in context from turn 1.
        with open_connections(ctx, session_id=session_id) as conns:
            assert conns.session_conn is not None
            store.persist_material(
                conns.session_conn,
                direction=Direction.OUT.value,
                stage=Stage.BOOTSTRAP.value,
                type=MaterialType.BUSINESS_BRIEF.value,
                content=RESTAURANT_SEED_BRIEF,
            )

        # Re-load the session row for response shape (need created_at, status).
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            session = store.load_session(conns.core_conn, session_id)
        assert session is not None

        body_out = {
            "session_id": session_id,
            "status": session["status"],
            "current_stage": session["current_stage"],
            "created_at": session["created_at"],
        }
        return JSONResponse(
            status_code=status.HTTP_201_CREATED, content=body_out
        )

    @app.get("/sessions")
    def list_sessions(
        ctx: AppContext = Depends(get_app_context),
    ) -> dict:
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            rows = store.list_sessions(conns.core_conn)
        return {"sessions": [_serialize_session(r) for r in rows]}

    @app.get("/sessions/{session_id}")
    def get_session(
        session_id: str,
        ctx: AppContext = Depends(get_app_context),
    ) -> dict:
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            session = store.load_session(conns.core_conn, session_id)
            if session is None:
                raise _not_found(
                    "not_found", {"session_id": session_id}
                )
            spend_summary = store.spend_summary_for_session(
                conns.core_conn, session_id
            )

        with open_connections(ctx, session_id=session_id) as conns:
            assert conns.session_conn is not None
            events = store.load_events(conns.session_conn)
            checkpoints = store.load_checkpoints(conns.session_conn)
            unresolved = store.load_alarms(conns.session_conn, resolved=False)
            resolved = store.load_alarms(conns.session_conn, resolved=True)
            pending_materials = store.load_pending_materials(conns.session_conn)

        # Events: cap at 500 most recent (events are ordered ASC by id, which
        # is chronological because UUID7). Keep ascending order in output.
        if len(events) > _EVENTS_CAP:
            events = events[-_EVENTS_CAP:]

        # Alarms ordering: unresolved first, then resolved (each sub-list
        # already sorted ASC by created_at because load_alarms orders that way).
        alarms_ordered = list(unresolved) + list(resolved)

        return {
            "session": _serialize_session(session),
            "events": [_serialize_event(e) for e in events],
            "checkpoints": [_serialize_checkpoint(c) for c in checkpoints],
            "alarms": [_serialize_alarm(a) for a in alarms_ordered],
            "pending_materials": [
                _serialize_material(m) for m in pending_materials
            ],
            "spend_summary": spend_summary,
        }

    @app.post("/sessions/{session_id}/resume")
    def resume_session(
        session_id: str,
        body: ResumeRequest,
        ctx: AppContext = Depends(get_app_context),
    ) -> dict:
        # 404 if session missing. Spec: no 409 if not 'active' — orchestrator
        # no-ops on terminal/awaiting sessions and we return its RunResult.
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            session = store.load_session(conns.core_conn, session_id)
        if session is None:
            raise _not_found("not_found", {"session_id": session_id})

        result = _run_loop_for(ctx, session_id)
        return _serialize_run_result(result)

    @app.post("/sessions/{session_id}/answer")
    def answer_session(
        session_id: str,
        body: AnswerRequest,
        ctx: AppContext = Depends(get_app_context),
    ) -> dict:
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            session = store.load_session(conns.core_conn, session_id)
        if session is None:
            raise _not_found("not_found", {"session_id": session_id})

        with open_connections(ctx, session_id=session_id) as conns:
            assert conns.core_conn is not None
            assert conns.session_conn is not None
            pending = store.load_material(conns.session_conn, body.material_id)
            if pending is None:
                raise _not_found(
                    "material_not_found",
                    {"session_id": session_id, "material_id": body.material_id},
                )
            if not pending.get("pending"):
                raise _bad_request(
                    "material_not_pending",
                    {
                        "session_id": session_id,
                        "material_id": body.material_id,
                    },
                )

            stage = session["current_stage"]
            content = pending.get("content")
            is_approval = (
                isinstance(content, dict) and content.get("kind") == "approval"
            )

            if is_approval:
                if body.approved is None:
                    raise _bad_request(
                        "bad_request",
                        {
                            "reason": (
                                "pending material is an approval; body must"
                                " include `approved`"
                            ),
                        },
                    )
                subject = (
                    content.get("subject") if isinstance(content, dict) else None
                )
                answer_content: dict[str, Any] = {
                    "approved": bool(body.approved),
                    "subject": subject,
                    "kind": "approval",
                    "notes": body.notes,
                }
                answer_mid = store.persist_material(
                    conns.session_conn,
                    direction=Direction.IN.value,
                    stage=stage,
                    type=MaterialType.USER_APPROVAL.value,
                    content=answer_content,
                )
            else:
                if body.answer_text is None:
                    raise _bad_request(
                        "bad_request",
                        {
                            "reason": (
                                "pending material is a question; body must"
                                " include `answer_text`"
                            ),
                        },
                    )
                answer_content = {"answer_text": body.answer_text}
                answer_mid = store.persist_material(
                    conns.session_conn,
                    direction=Direction.IN.value,
                    stage=stage,
                    type=MaterialType.USER_ANSWER.value,
                    content=answer_content,
                )

            store.mark_material_resolved(conns.session_conn, body.material_id)
            store.append_event(
                conns.session_conn,
                type=EventType.HUMAN_RESUMED.value,
                stage=stage,
                payload={
                    "material_id": answer_mid,
                    "answer_or_decision": answer_content,
                },
                material_id=answer_mid,
            )
            store.update_session_status(
                conns.core_conn,
                session_id,
                SessionStatus.ACTIVE.value,
            )

        result = _run_loop_for(ctx, session_id)
        return _serialize_run_result(result)

    # -------------------- HTML routes (web UI) --------------------

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        ctx: AppContext = Depends(get_app_context),
    ):
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            rows = store.list_sessions(conns.core_conn)
        sessions = [_serialize_session(r) for r in rows]
        return _templates.TemplateResponse(
            request,
            "index.html",
            {"sessions": sessions},
        )

    @app.get("/sessions/{session_id}/view", response_class=HTMLResponse)
    def view_session(
        session_id: str,
        request: Request,
        partial: bool = False,
        ctx: AppContext = Depends(get_app_context),
    ):
        with open_connections(ctx) as conns:
            assert conns.core_conn is not None
            session = store.load_session(conns.core_conn, session_id)
            if session is None:
                raise _not_found("not_found", {"session_id": session_id})
            spend_summary = store.spend_summary_for_session(
                conns.core_conn, session_id
            )

        with open_connections(ctx, session_id=session_id) as conns:
            assert conns.session_conn is not None
            events_all = store.load_events(conns.session_conn)
            checkpoints = store.load_checkpoints(conns.session_conn)
            unresolved = store.load_alarms(conns.session_conn, resolved=False)
            resolved = store.load_alarms(conns.session_conn, resolved=True)
            pending_materials = store.load_pending_materials(conns.session_conn)

        # Cap events at 50 most recent for display; flag truncation.
        events_total = len(events_all)
        events_truncated = events_total > _UI_EVENTS_CAP
        events = events_all[-_UI_EVENTS_CAP:] if events_truncated else events_all
        # Pre-compute a short summary per event so templates stay logic-free.
        event_summaries = [_summarize_event_payload(e) for e in events]

        alarms_ordered = list(unresolved) + list(resolved)

        models = _models_from_env()

        # Site preview: enable iframe if data/sites/{id}/index.html exists.
        site_index = ctx.sites_dir / session_id / "index.html"
        has_site_preview = site_index.is_file()

        context = {
            "session": _serialize_session(session),
            "events": [_serialize_event(e) for e in events],
            "events_total": events_total,
            "events_truncated": events_truncated,
            "event_summaries": event_summaries,
            "checkpoints": [_serialize_checkpoint(c) for c in checkpoints],
            "alarms": [_serialize_alarm(a) for a in alarms_ordered],
            "pending_materials": [
                _serialize_material(m) for m in pending_materials
            ],
            "spend_summary": spend_summary,
            "models": models,
            "has_site_preview": has_site_preview,
        }
        template_name = "_session_main.html" if partial else "session.html"
        return _templates.TemplateResponse(request, template_name, context)

    # -------------------- exception handlers --------------------

    @app.exception_handler(_ApiError)
    def _handle_api_error(_request: Request, exc: _ApiError) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return _error_response(
            status_code=exc.status_code, error=exc.error_code, detail=detail
        )

    @app.exception_handler(RequestValidationError)
    def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            error="bad_request",
            detail={"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(json.JSONDecodeError)
    def _handle_json_decode(_request: Request, exc: json.JSONDecodeError) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            error="bad_request",
            detail={"reason": "request body is not valid JSON"},
        )


# ---------------------------------------------------------------------------
# Internal: build + invoke the orchestrator for one session
# ---------------------------------------------------------------------------


def _run_loop_for(ctx: AppContext, session_id: str) -> RunResult:
    """Open per-call connections, build the orchestrator config, run the loop.

    Connections opened here are SOLELY for building the worker (LLMWorker
    needs core_conn + session_conn to log spend and append `model_swapped`
    events). The orchestrator opens its own short-lived pair internally.
    """
    # Build worker_for_stage with connections we keep open for the loop run.
    # The orchestrator opens its own pair; that's fine — SQLite handles two
    # readers/writers on the same file with WAL/serialized mode.
    core_conn: sqlite3.Connection = store.core_connection(ctx.core_db_path)
    session_conn: sqlite3.Connection = store.session_connection(
        ctx.sessions_dir, session_id
    )
    try:
        worker_for_stage = ctx.worker_for_stage_factory(
            session_id=session_id,
            core_conn=core_conn,
            session_conn=session_conn,
            llm_client=ctx.llm_client,
        )
        config = make_orchestrator_config(
            session_id=session_id,
            core_conn=core_conn,
            session_conn=session_conn,
            llm_client=ctx.llm_client,
            sandbox_root=ctx.sites_dir,
            core_db_path=ctx.core_db_path,
            sessions_dir=ctx.sessions_dir,
            worker_for_stage_override=worker_for_stage,
        )
        return run_until_pause(session_id, config)
    finally:
        session_conn.close()
        core_conn.close()


# ---------------------------------------------------------------------------
# App factory + module-level instance
# ---------------------------------------------------------------------------


def create_app(app_context: AppContext | None = None) -> FastAPI:
    """Construct the FastAPI app.

    If `app_context` is None, the default production AppContext is built. The
    AppContext is stashed on `app.state.app_context` so `get_app_context` can
    retrieve it from any handler via FastAPI's Depends().
    """
    fastapi_app = FastAPI(title="harness", version="0.1.0")
    ctx = (
        app_context if app_context is not None else build_default_app_context()
    )
    fastapi_app.state.app_context = ctx
    _create_app_routes(fastapi_app)
    # Static mount for generated sites — served at /sites/{session_id}/...
    # The directory is guaranteed to exist by build_default_app_context (or
    # by the test fixture in tests/api/test_web_ui.py).
    ctx.sites_dir.mkdir(parents=True, exist_ok=True)
    fastapi_app.mount(
        "/sites", StaticFiles(directory=str(ctx.sites_dir)), name="sites"
    )
    return fastapi_app


# Module-level app for `uvicorn harness.api.app:app`. Built lazily-free
# (build_default_app_context only creates directories + an httpx client).
app: FastAPI = create_app()


__all__ = ["app", "create_app"]
