"""FastAPI dependencies for the HTTP layer.

`AppContext` is the per-process configuration the route handlers need:
filesystem paths for the DBs and the site sandbox root, the OpenRouter
client, and the factory that builds the stage-to-Worker map. Tests inject a
custom `worker_for_stage_factory` to substitute a MockWorker.

Layer rule: this module may import stdlib + fastapi + every harness.services.*
and harness.domain.* (since domain is at the same layer, the API layer is the
composition root). It MUST NOT import from harness.templates.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Request

from harness.domain.website_builder import make_worker_for_stage
from harness.services.llm import OpenRouterClient
from harness.services.worker import Worker


# Default data directory; relative to the working directory unless overridden.
_DEFAULT_DATA_DIR = Path("data")


WorkerForStageFactory = Callable[..., Callable[[str], Worker]]


@dataclass
class AppContext:
    """Per-process configuration handed to every request handler.

    `worker_for_stage_factory` has the same signature as
    `domain.make_worker_for_stage` — tests override it with a closure that
    ignores the LLM client + model args and hands back a fixed MockWorker for
    every stage.
    """

    core_db_path: Path
    sessions_dir: Path
    sites_dir: Path
    llm_client: OpenRouterClient
    worker_for_stage_factory: WorkerForStageFactory = field(
        default=make_worker_for_stage
    )


def build_default_app_context(
    *,
    data_dir: Path | None = None,
) -> AppContext:
    """Construct the production AppContext.

    Resolves `data_dir` from the `HARNESS_DATA_DIR` env var (else the literal
    default `./data`), creates the sub-directories, and instantiates a real
    OpenRouterClient (which itself reads `OPENROUTER_API_KEY` lazily at call
    time, so building this context does not require the key to be set).
    """
    if data_dir is None:
        env_dir = os.environ.get("HARNESS_DATA_DIR")
        data_dir = Path(env_dir) if env_dir else _DEFAULT_DATA_DIR
    data_dir = Path(data_dir)

    sessions_dir = data_dir / "sessions"
    sites_dir = data_dir / "sites"
    core_db_path = data_dir / "harness.db"

    sessions_dir.mkdir(parents=True, exist_ok=True)
    sites_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    return AppContext(
        core_db_path=core_db_path,
        sessions_dir=sessions_dir,
        sites_dir=sites_dir,
        llm_client=OpenRouterClient(),
        worker_for_stage_factory=make_worker_for_stage,
    )


def get_app_context(request: Request) -> AppContext:
    """FastAPI Depends() target: return the AppContext stashed on app.state."""
    ctx = getattr(request.app.state, "app_context", None)
    if ctx is None:
        raise RuntimeError(
            "AppContext is not attached to app.state; build with create_app()"
        )
    if not isinstance(ctx, AppContext):
        raise RuntimeError(
            f"app.state.app_context is not an AppContext (got {type(ctx)!r})"
        )
    return ctx


# ---------------------------------------------------------------------------
# Per-request connection helpers
# ---------------------------------------------------------------------------


class _ConnectionPair:
    """Context-manager that opens core + (optional) session connections.

    Tiny helper so routes don't repeat the open/close try/finally dance. The
    `session_id` may be None when the route only needs the core DB (e.g.
    GET /sessions).
    """

    def __init__(
        self,
        *,
        core_db_path: Path,
        sessions_dir: Path,
        session_id: str | None = None,
    ) -> None:
        self._core_db_path = core_db_path
        self._sessions_dir = sessions_dir
        self._session_id = session_id
        self.core_conn: sqlite3.Connection | None = None
        self.session_conn: sqlite3.Connection | None = None

    def __enter__(self) -> "_ConnectionPair":
        # Local import keeps the layer boundary tight: store is service-layer.
        from harness.services import store

        self.core_conn = store.core_connection(self._core_db_path)
        if self._session_id is not None:
            self.session_conn = store.session_connection(
                self._sessions_dir, self._session_id
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.session_conn is not None:
            self.session_conn.close()
            self.session_conn = None
        if self.core_conn is not None:
            self.core_conn.close()
            self.core_conn = None


def open_connections(
    app_context: AppContext,
    *,
    session_id: str | None = None,
) -> _ConnectionPair:
    """Open core + (optional) session connections; close on context exit."""
    return _ConnectionPair(
        core_db_path=app_context.core_db_path,
        sessions_dir=app_context.sessions_dir,
        session_id=session_id,
    )


__all__ = [
    "AppContext",
    "WorkerForStageFactory",
    "build_default_app_context",
    "get_app_context",
    "open_connections",
]
