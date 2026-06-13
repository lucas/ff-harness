"""Shared fixtures for Step 1+ tests. Kept lean; later steps will extend."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.services import store


@pytest.fixture
def tmp_core_conn(tmp_path: Path):
    conn = store.core_connection(tmp_path / "harness.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def tmp_sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def tmp_session(tmp_core_conn, tmp_sessions_dir: Path):
    """Create a session in the core DB and open its per-session DB.

    Yields (core_conn, session_conn, session_id).
    """
    session_id = store.create_session(tmp_core_conn)
    session_conn = store.session_connection(tmp_sessions_dir, session_id)
    try:
        yield tmp_core_conn, session_conn, session_id
    finally:
        session_conn.close()


@pytest.fixture
def sandbox_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sandbox"
    d.mkdir(parents=True, exist_ok=True)
    return d
