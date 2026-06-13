from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.services import guardrails


# ---------------------------------------------------------------------------
# is_tool_allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,allow_list,expected",
    [
        ("write_file", ["write_file", "read_file"], True),
        ("read_file", ["write_file", "read_file"], True),
        ("delete_file", ["write_file", "read_file"], False),
        # Case sensitivity
        ("Write_File", ["write_file"], False),
        ("WRITE_FILE", ["write_file"], False),
        # Empty allow-list denies everything
        ("write_file", [], False),
        ("", [], False),
        # Empty string can be explicitly allowed (edge case)
        ("", [""], True),
        # Iterable other than list works (set, tuple, generator)
        ("write_file", ("write_file",), True),
        ("write_file", {"write_file", "read_file"}, True),
    ],
)
def test_is_tool_allowed(tool, allow_list, expected):
    assert guardrails.is_tool_allowed(tool, allow_list) is expected


# ---------------------------------------------------------------------------
# is_path_safe
# ---------------------------------------------------------------------------


def test_is_path_safe_sandbox_root_itself(tmp_path: Path):
    assert guardrails.is_path_safe(tmp_path, tmp_path) is True


def test_is_path_safe_in_sandbox_file(tmp_path: Path):
    inside = tmp_path / "site.html"
    assert guardrails.is_path_safe(inside, tmp_path) is True


def test_is_path_safe_nested_deep_file(tmp_path: Path):
    nested = tmp_path / "assets" / "css" / "styles.css"
    assert guardrails.is_path_safe(nested, tmp_path) is True


def test_is_path_safe_rejects_dot_dot_escape(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    escape = sandbox / ".." / "secret.txt"
    assert guardrails.is_path_safe(escape, sandbox) is False


def test_is_path_safe_rejects_absolute_outside(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    other = tmp_path / "other" / "file.txt"
    assert guardrails.is_path_safe(other, sandbox) is False


def test_is_path_safe_rejects_root_absolute(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    assert guardrails.is_path_safe("/etc/passwd", sandbox) is False


def test_is_path_safe_rejects_symlink_escape(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("classified")
    link = sandbox / "leak"
    os.symlink(secret, link)
    assert guardrails.is_path_safe(link, sandbox) is False


def test_is_path_safe_accepts_string_paths(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    inside = sandbox / "index.html"
    assert guardrails.is_path_safe(str(inside), str(sandbox)) is True


# ---------------------------------------------------------------------------
# check_turn_cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "iter_count,max_iters,expected",
    [
        (0, 10, False),
        (1, 10, False),
        (9, 10, False),
        (10, 10, True),
        (11, 10, True),
        (100, 10, True),
        # Custom cap
        (4, 5, False),
        (5, 5, True),
        (6, 5, True),
        # Negative inputs are not "cap reached"
        (-1, 10, False),
        (-100, 10, False),
    ],
)
def test_check_turn_cap(iter_count, max_iters, expected):
    assert guardrails.check_turn_cap(iter_count, max_iters) is expected


def test_check_turn_cap_default_max():
    assert guardrails.check_turn_cap(9) is False
    assert guardrails.check_turn_cap(10) is True


# ---------------------------------------------------------------------------
# check_spend_cap_today
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spent,cap,expected",
    [
        (0.0, 1.0, False),
        (0.99, 1.0, False),
        (1.0, 1.0, True),
        (1.5, 1.0, True),
        # Custom cap
        (4.99, 5.0, False),
        (5.0, 5.0, True),
        # Negative spend is not "cap reached"
        (-0.5, 1.0, False),
    ],
)
def test_check_spend_cap_today(spent, cap, expected):
    assert guardrails.check_spend_cap_today(spent, cap) is expected


def test_check_spend_cap_today_default_cap():
    assert guardrails.check_spend_cap_today(0.5) is False
    assert guardrails.check_spend_cap_today(1.0) is True
    assert guardrails.check_spend_cap_today(2.0) is True
