"""Step 5 gate — `harness.services.post_hooks.run` chain.

Asserts the chain always completes (validate -> SEO -> git) even on partial
failure, that SEO artifacts are regenerated correctly, and that the git
repo is lazily initialized and committed to.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from harness.services import post_hooks


SHA40 = re.compile(r"^[0-9a-f]{40}$")

GOOD_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Index</title>
    <meta name="viewport" content="width=device-width">
  </head>
  <body><h1>Hello</h1></body>
</html>
"""

# Missing DOCTYPE so html5lib strict mode raises.
BAD_HTML = "<title>x</title><body><h1>hi</h1></body>"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


# ---------------------------------------------------------------------------
# Stage outcomes
# ---------------------------------------------------------------------------


def test_run_on_empty_sandbox_initializes_git_and_commits_seo(sandbox_dir: Path):
    assert not (sandbox_dir / ".git").exists()

    report = post_hooks.run(sandbox_dir)

    # Stage 1 — vacuously OK (no files to validate).
    assert report.validate_ok is True
    assert report.html_reports == {}
    assert report.css_reports == {}

    # Stage 2 — SEO files written.
    for name in ("sitemap.xml", "robots.txt", "llms.txt"):
        assert (sandbox_dir / name).is_file(), f"{name} missing"
    assert report.seo_regenerated is True
    assert report.seo_report["sitemap_xml_valid"] is True
    assert report.seo_report["sitemap_xml_url_count"] == 0
    assert report.seo_report["robots_txt_present"] is True
    assert report.seo_report["llms_txt_present"] is True

    # Stage 3 — git initialized and committed.
    assert (sandbox_dir / ".git").is_dir()
    assert report.git_commit_sha is not None
    assert SHA40.fullmatch(report.git_commit_sha)
    assert report.git_message == "committed"
    assert report.errors == []

    log = _git(sandbox_dir, "log", "--oneline")
    assert log.returncode == 0
    assert "auto: post-hook iteration" in log.stdout


def test_run_with_one_good_html_validates_and_sitemap_lists_it(sandbox_dir: Path):
    (sandbox_dir / "index.html").write_text(GOOD_HTML, encoding="utf-8")

    report = post_hooks.run(sandbox_dir)

    assert "index.html" in report.html_reports
    h = report.html_reports["index.html"]
    assert h["html5_parses"] is True
    assert h["has_title"] and h["has_meta_viewport"] and h["has_lang"] and h["has_h1"]
    assert report.validate_ok is True

    sitemap = (sandbox_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>http://localhost:8000/index.html</loc>" in sitemap
    assert report.seo_report["sitemap_xml_url_count"] == 1

    assert report.git_commit_sha is not None
    assert SHA40.fullmatch(report.git_commit_sha)
    assert report.errors == []


def test_run_with_bad_html_still_completes_full_chain(sandbox_dir: Path):
    (sandbox_dir / "index.html").write_text(BAD_HTML, encoding="utf-8")

    report = post_hooks.run(sandbox_dir)

    # Stage 1 fails on the bad file...
    assert "index.html" in report.html_reports
    assert report.html_reports["index.html"]["html5_parses"] is False
    assert report.html_reports["index.html"]["html5_errors"]
    assert report.validate_ok is False

    # ...but stages 2 and 3 still complete.
    assert (sandbox_dir / "sitemap.xml").is_file()
    assert (sandbox_dir / "robots.txt").is_file()
    assert (sandbox_dir / "llms.txt").is_file()
    assert report.seo_regenerated is True

    assert report.git_commit_sha is not None
    assert SHA40.fullmatch(report.git_commit_sha)
    # `errors` is reserved for stage-level crashes — per-file validation
    # failures live in the html/css reports, not here.
    assert report.errors == []


def test_run_twice_second_call_has_nothing_to_commit(sandbox_dir: Path):
    (sandbox_dir / "index.html").write_text(GOOD_HTML, encoding="utf-8")

    first = post_hooks.run(sandbox_dir)
    assert first.git_commit_sha is not None

    second = post_hooks.run(sandbox_dir)
    # Same content -> SEO regen writes identical files -> nothing to commit.
    assert second.git_commit_sha is None
    assert second.git_message == "nothing to commit"
    assert second.errors == []
    # Validation/SEO still ran cleanly.
    assert second.validate_ok is True
    assert second.seo_regenerated is True


def test_run_with_html_and_css_validates_both_and_sitemap_excludes_css(
    sandbox_dir: Path,
):
    (sandbox_dir / "index.html").write_text(GOOD_HTML, encoding="utf-8")
    (sandbox_dir / "styles.css").write_text("body { color: red; }\n", encoding="utf-8")

    report = post_hooks.run(sandbox_dir)

    assert "index.html" in report.html_reports
    assert "styles.css" in report.css_reports
    assert report.css_reports["styles.css"]["css_parses"] is True
    assert report.validate_ok is True

    # CSS is not a page; sitemap should list only the HTML.
    assert report.seo_report["sitemap_xml_url_count"] == 1
    sitemap = (sandbox_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "styles.css" not in sitemap


def test_run_validates_html_in_subdirectories(sandbox_dir: Path):
    sub = sandbox_dir / "pages"
    sub.mkdir()
    (sandbox_dir / "index.html").write_text(GOOD_HTML, encoding="utf-8")
    (sub / "about.html").write_text(GOOD_HTML, encoding="utf-8")

    report = post_hooks.run(sandbox_dir)

    assert "index.html" in report.html_reports
    # Either OS separator works; normalise for comparison.
    assert any(
        k.replace("\\", "/") == "pages/about.html" for k in report.html_reports
    )
    assert report.seo_report["sitemap_xml_url_count"] == 2


def test_run_with_bad_css_marks_validate_ok_false_but_still_commits(
    sandbox_dir: Path,
):
    (sandbox_dir / "styles.css").write_text("@@@invalid", encoding="utf-8")
    report = post_hooks.run(sandbox_dir)
    assert report.css_reports["styles.css"]["css_parses"] is False
    assert report.validate_ok is False
    assert report.git_commit_sha is not None
    assert report.errors == []


def test_run_skips_git_internal_files_from_validation(sandbox_dir: Path):
    # First call inits git and creates SEO + commits.
    post_hooks.run(sandbox_dir)
    # Sanity: .git exists and may contain HTML/CSS-looking blob paths in theory,
    # but the canonical case is just verifying no .git-internal file ends up
    # in the reports dict.
    (sandbox_dir / "index.html").write_text(GOOD_HTML, encoding="utf-8")
    report = post_hooks.run(sandbox_dir)
    for key in list(report.html_reports) + list(report.css_reports):
        assert not key.startswith(".git/"), key


def test_base_url_propagates_to_sitemap_and_robots(sandbox_dir: Path):
    (sandbox_dir / "index.html").write_text(GOOD_HTML, encoding="utf-8")
    report = post_hooks.run(sandbox_dir, base_url="https://example.com/")
    assert report.seo_regenerated is True

    sitemap = (sandbox_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://example.com/index.html</loc>" in sitemap

    robots = (sandbox_dir / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://example.com/sitemap.xml" in robots


@pytest.mark.parametrize("base_url", ["http://x", "http://x/"])
def test_base_url_trailing_slash_normalized(sandbox_dir: Path, base_url: str):
    (sandbox_dir / "index.html").write_text(GOOD_HTML, encoding="utf-8")
    post_hooks.run(sandbox_dir, base_url=base_url)
    sitemap = (sandbox_dir / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>http://x/index.html</loc>" in sitemap
