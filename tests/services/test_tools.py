"""Step 4 gate — every worker-callable tool + the dispatcher that guards them."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.models.enums import AlarmType, MaterialType
from harness.services import store
from harness.services.tools import (
    REGISTRY,
    ToolContext,
    ToolResult,
    dispatch,
    files as files_mod,
)


ALL_TOOLS = [
    "ask_user",
    "request_approval",
    "read_file",
    "write_file",
    "list_files",
    "render_mockup",
]


def _ok(result: ToolResult) -> dict:
    """Assert + narrow result.result to dict for pyright."""
    assert result.ok is True, f"expected ok=True, got error={result.error}"
    assert result.result is not None
    return result.result


def _err(result: ToolResult) -> dict:
    """Assert + narrow result.error to dict for pyright."""
    assert result.ok is False, "expected ok=False"
    assert result.error is not None
    return result.error


def make_ctx(
    tmp_session,
    sandbox_dir: Path,
    *,
    allow_list: list[str] | None = None,
    stage: str = "build",
) -> ToolContext:
    _core, session_conn, _sid = tmp_session
    return ToolContext(
        session_conn=session_conn,
        sandbox_path=sandbox_dir,
        stage=stage,
        allow_list=list(allow_list) if allow_list is not None else list(ALL_TOOLS),
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_denies_non_allowlisted_tool(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir, allow_list=["read_file"])

    result = dispatch("write_file", {"path": "x.html", "content": "hi"}, ctx)

    assert _err(result)["error_kind"] == "denied_by_allowlist"

    alarms = store.load_alarms(session_conn)
    assert len(alarms) == 1
    assert alarms[0]["type"] == AlarmType.TOOL_FAILED.value
    assert alarms[0]["context"]["tool"] == "write_file"
    assert alarms[0]["context"]["error_kind"] == "denied_by_allowlist"


def test_dispatch_blocks_path_escape_on_write_file(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    result = dispatch(
        "write_file",
        {"path": "../escape.html", "content": "<html></html>"},
        ctx,
    )

    assert _err(result)["error_kind"] == "path_escape"

    # And the file was NOT written.
    escape_target = (sandbox_dir.parent / "escape.html").resolve(strict=False)
    assert not escape_target.exists()

    alarms = store.load_alarms(session_conn)
    assert len(alarms) == 1
    assert alarms[0]["context"]["error_kind"] == "path_escape"


def test_dispatch_blocks_path_escape_on_read_file(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    result = dispatch("read_file", {"path": "../outside.txt"}, ctx)

    assert _err(result)["error_kind"] == "path_escape"
    assert len(store.load_alarms(session_conn)) == 1


def test_dispatch_swallows_tool_exception(tmp_session, sandbox_dir, monkeypatch):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    def _boom(_args, _ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(REGISTRY, "render_mockup", _boom)

    result = dispatch("render_mockup", {"layout_spec": {"sections": [{"name": "x"}]}}, ctx)

    err = _err(result)
    assert err["error_kind"] == "tool_exception"
    assert "kaboom" in err["error_message"]

    alarms = store.load_alarms(session_conn)
    assert len(alarms) == 1
    assert alarms[0]["context"]["error_kind"] == "tool_exception"


def test_dispatch_alarms_when_allowed_but_not_in_registry(
    tmp_session, sandbox_dir, monkeypatch
):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir, allow_list=["phantom_tool"])

    # phantom_tool is allow-listed but absent from REGISTRY -> programmer-error
    # path; dispatcher still must not raise.
    result = dispatch("phantom_tool", {}, ctx)

    assert _err(result)["error_kind"] == "not_implemented"

    alarms = store.load_alarms(session_conn)
    assert len(alarms) == 1
    assert alarms[0]["context"]["error_kind"] == "not_implemented"


def test_dispatch_happy_path_returns_tool_result(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    result = dispatch("ask_user", {"question": "Restaurant name?"}, ctx)

    assert result.pause_reason == "awaiting_human"
    assert "material_id" in _ok(result)


# ---------------------------------------------------------------------------
# ask_user / request_approval
# ---------------------------------------------------------------------------


def test_ask_user_persists_pending_question_and_pauses(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir, stage="bootstrap")

    result = dispatch(
        "ask_user",
        {"question": "What is your name?", "options": ["Maria", "Other"]},
        ctx,
    )

    assert result.pause_reason == "awaiting_human"
    mid = _ok(result)["material_id"]

    pending = store.load_pending_materials(session_conn)
    assert len(pending) == 1
    row = pending[0]
    assert row["id"] == mid
    assert row["type"] == MaterialType.PENDING_QUESTION.value
    assert row["direction"] == "out"
    assert row["stage"] == "bootstrap"
    assert row["pending"] is True
    assert row["content"] == {
        "question": "What is your name?",
        "options": ["Maria", "Other"],
    }


def test_ask_user_bad_args_returns_failure(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    result = dispatch("ask_user", {"question": ""}, ctx)

    assert _err(result)["error_kind"] == "bad_args"
    # bad_args from inside the tool is NOT alarmed by the dispatcher (only
    # exception/escape/allow-list/registry failures alarm). The tool simply
    # returns it as data.
    assert store.load_alarms(session_conn) == []
    assert store.load_pending_materials(session_conn) == []


def test_request_approval_persists_pending_question_with_kind_approval(
    tmp_session, sandbox_dir
):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")

    result = dispatch(
        "request_approval",
        {"subject": "Approve mockup?", "details": {"sections": ["Header", "Hero"]}},
        ctx,
    )

    assert result.pause_reason == "awaiting_human"
    mid = _ok(result)["material_id"]

    row = store.load_material(session_conn, mid)
    assert row is not None
    assert row["type"] == MaterialType.PENDING_QUESTION.value
    assert row["pending"] is True
    assert row["content"] == {
        "kind": "approval",
        "subject": "Approve mockup?",
        "details": {"sections": ["Header", "Hero"]},
    }


def test_request_approval_bad_args(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)

    missing = dispatch("request_approval", {}, ctx)
    assert _err(missing)["error_kind"] == "bad_args"

    bad_details = dispatch(
        "request_approval", {"subject": "ok", "details": "not-a-dict"}, ctx
    )
    assert _err(bad_details)["error_kind"] == "bad_args"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


def test_write_file_writes_to_disk_under_sandbox(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    body = "<!doctype html><title>x</title>"
    result = dispatch("write_file", {"path": "index.html", "content": body}, ctx)

    assert _ok(result)["bytes"] == len(body.encode("utf-8"))
    written = sandbox_dir / "index.html"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == body


def test_write_file_creates_parent_dirs(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    result = dispatch(
        "write_file",
        {"path": "css/site/main.css", "content": "body { color: black; }"},
        ctx,
    )
    _ok(result)
    nested = sandbox_dir / "css" / "site" / "main.css"
    assert nested.exists()


def test_write_file_persists_site_file_material(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    body = "hello-world"
    result = dispatch("write_file", {"path": "a.txt", "content": body}, ctx)
    mid = _ok(result)["material_id"]

    row = store.load_material(session_conn, mid)
    assert row is not None
    assert row["type"] == MaterialType.SITE_FILE.value
    assert row["direction"] == "out"
    assert row["pending"] is False
    assert row["content"]["path"] == "a.txt"
    assert row["content"]["bytes"] == len(body.encode("utf-8"))
    # 16-char prefix of sha256, deterministic for this body.
    assert len(row["content"]["content_hash"]) == 16


def test_write_file_sandbox_escape_does_not_write(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    before = list(sandbox_dir.rglob("*"))
    result = dispatch(
        "write_file", {"path": "../sneaky.txt", "content": "boom"}, ctx
    )

    assert _err(result)["error_kind"] == "path_escape"
    # sandbox is untouched
    assert list(sandbox_dir.rglob("*")) == before
    # and no site_file material was persisted
    rows = [
        m
        for m in store.load_pending_materials(session_conn)
        if m["type"] == MaterialType.SITE_FILE.value
    ]
    assert rows == []


def test_write_file_bad_args(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    r1 = dispatch("write_file", {"content": "x"}, ctx)
    assert _err(r1)["error_kind"] == "bad_args"
    r2 = dispatch("write_file", {"path": "ok.txt"}, ctx)
    assert _err(r2)["error_kind"] == "bad_args"


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def test_read_file_roundtrips_write(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    body = "abc 123 ☃"
    dispatch("write_file", {"path": "note.txt", "content": body}, ctx)

    result = dispatch("read_file", {"path": "note.txt"}, ctx)

    out = _ok(result)
    assert out["content"] == body
    assert out["bytes"] == len(body.encode("utf-8"))


def test_read_file_not_found(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    result = dispatch("read_file", {"path": "missing.txt"}, ctx)

    assert _err(result)["error_kind"] == "not_found"


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def test_list_files_lists_relative_sorted_files_only(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    dispatch("write_file", {"path": "b.txt", "content": "b"}, ctx)
    dispatch("write_file", {"path": "a.txt", "content": "a"}, ctx)
    dispatch("write_file", {"path": "sub/c.txt", "content": "c"}, ctx)

    result = dispatch("list_files", {}, ctx)

    out = _ok(result)
    files = out["files"]
    assert files == sorted(files)
    # No directory entries.
    assert "sub" not in files
    assert {"a.txt", "b.txt", "sub/c.txt"}.issubset(set(files))
    assert out["truncated"] is False


def test_list_files_truncates_at_cap(tmp_session, sandbox_dir, monkeypatch):
    ctx = make_ctx(tmp_session, sandbox_dir)
    for i in range(10):
        dispatch("write_file", {"path": f"f{i:02d}.txt", "content": "x"}, ctx)

    monkeypatch.setattr(files_mod, "_list_cap", lambda: 5)
    result = dispatch("list_files", {}, ctx)

    out = _ok(result)
    assert len(out["files"]) == 5
    assert out["truncated"] is True


def test_list_files_rejects_escape(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir)

    result = dispatch("list_files", {"path": "../"}, ctx)

    assert _err(result)["error_kind"] == "path_escape"
    assert len(store.load_alarms(session_conn)) == 1


# ---------------------------------------------------------------------------
# render_mockup
# ---------------------------------------------------------------------------


def _spec() -> dict:
    return {
        "sections": [
            {"name": "Header"},
            {"name": "Hero"},
            {"name": "Menu"},
            {"name": "Footer"},
        ],
        "primary_cta": "Reserve",
    }


def test_render_mockup_is_deterministic(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    r1 = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    r2 = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    assert _ok(r1)["ascii"] == _ok(r2)["ascii"]


def test_render_mockup_includes_every_section_in_regions(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir)
    result = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    out = _ok(result)
    assert out["regions"] == ["Header", "Hero", "Menu", "Footer"]
    # Every region name is visible in the ASCII output.
    for name in ["Header", "Hero", "Menu", "Footer"]:
        assert name in out["ascii"]
    assert "[Reserve]" in out["ascii"]


def test_render_mockup_persists_mockup_material(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")

    result = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    out = _ok(result)
    mid = out["material_id"]

    row = store.load_material(session_conn, mid)
    assert row is not None
    assert row["type"] == MaterialType.MOCKUP.value
    assert row["stage"] == "mockup"
    assert row["pending"] is False
    assert row["content"]["regions"] == ["Header", "Hero", "Menu", "Footer"]
    assert row["content"]["ascii"] == out["ascii"]


@pytest.mark.parametrize(
    "spec, kind",
    [
        ({}, "bad_args"),
        ({"sections": []}, "bad_args"),
        ({"sections": [{"name": ""}]}, "bad_args"),
        ({"sections": [{"not_name": "x"}]}, "bad_args"),
    ],
)
def test_render_mockup_bad_spec(tmp_session, sandbox_dir, spec, kind):
    ctx = make_ctx(tmp_session, sandbox_dir)
    result = dispatch("render_mockup", {"layout_spec": spec}, ctx)
    assert _err(result)["error_kind"] == kind


# ---------------------------------------------------------------------------
# render_mockup — themed HTML output (the iframe-preview pipeline)
# ---------------------------------------------------------------------------


def _persist_brief(session_conn, content: dict) -> str:
    """Helper: write a business_brief material into the session DB."""
    return store.persist_material(
        session_conn,
        direction="out",
        stage="bootstrap",
        type=MaterialType.BUSINESS_BRIEF.value,
        content=content,
        pending=False,
    )


def test_render_mockup_includes_html_in_result_and_material(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")

    result = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    out = _ok(result)

    # Result carries the html doc.
    assert "html" in out
    assert isinstance(out["html"], str) and out["html"]
    assert out["html"].lower().startswith("<!doctype")
    assert "themed" in out
    # No brief on file -> themed=False.
    assert out["themed"] is False

    # Material content carries html + themed alongside the original fields.
    row = store.load_material(session_conn, out["material_id"])
    assert row is not None
    content = row["content"]
    assert content["ascii"] == out["ascii"]
    assert content["regions"] == out["regions"]
    assert content["html"] == out["html"]
    assert content["themed"] is False


def test_render_mockup_html_is_themed_when_brief_present(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    _persist_brief(
        session_conn,
        {
            "name": "Maria's Pizzeria",
            "tagline": "Wood-fired pizza, made by hand.",
            "palette": {"primary": "#7B1E1E", "secondary": "#F5E9DA"},
        },
    )
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")

    result = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    out = _ok(result)

    assert out["themed"] is True
    html_doc = out["html"]
    # Brand name surfaces in the HTML (HTML-escaped — apostrophe -> &#x27;).
    assert "Maria&#x27;s Pizzeria" in html_doc
    # Tagline surfaces too.
    assert "Wood-fired pizza, made by hand." in html_doc
    # Palette hex values appear in the inline style block.
    assert "#7B1E1E" in html_doc
    assert "#F5E9DA" in html_doc


def test_render_mockup_html_is_neutral_without_brief(tmp_session, sandbox_dir):
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")

    result = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    out = _ok(result)

    assert out["themed"] is False
    html_doc = out["html"]
    # No brief-specific name leaks into the document.
    assert "Maria" not in html_doc
    # Document is still valid and complete.
    assert html_doc.lower().startswith("<!doctype")
    assert "</html>" in html_doc


def test_render_mockup_html_rejects_malicious_palette_value(
    tmp_session, sandbox_dir
):
    _core, session_conn, _sid = tmp_session
    _persist_brief(
        session_conn,
        {
            "name": "Demo",
            "palette": {
                "primary": "javascript:alert(1)",
                "secondary": "url(javascript:alert(2))",
            },
        },
    )
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")

    result = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    html_doc = _ok(result)["html"]

    # Non-hex values are rejected outright — they never reach the output.
    assert "javascript:" not in html_doc
    assert "alert(" not in html_doc


def test_render_mockup_html_escapes_brief_name(tmp_session, sandbox_dir):
    _core, session_conn, _sid = tmp_session
    _persist_brief(session_conn, {"name": "<script>x</script>"})
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")

    result = dispatch("render_mockup", {"layout_spec": _spec()}, ctx)
    html_doc = _ok(result)["html"]

    # No live <script> tag in the rendered HTML — the brief name is escaped.
    assert "<script>x</script>" not in html_doc
    assert "&lt;script&gt;x&lt;/script&gt;" in html_doc


def test_render_mockup_ascii_unchanged_when_brief_present(tmp_session, sandbox_dir):
    """ASCII output must remain byte-identical regardless of brief/themed state
    — the mockup_renders checkpoint and screen-reader path depend on it."""
    _core, session_conn, _sid = tmp_session
    ctx = make_ctx(tmp_session, sandbox_dir, stage="mockup")
    no_brief = _ok(dispatch("render_mockup", {"layout_spec": _spec()}, ctx))["ascii"]

    _persist_brief(
        session_conn,
        {
            "name": "Anything",
            "palette": {"primary": "#7B1E1E", "secondary": "#F5E9DA"},
        },
    )
    with_brief = _ok(dispatch("render_mockup", {"layout_spec": _spec()}, ctx))["ascii"]

    assert no_brief == with_brief
