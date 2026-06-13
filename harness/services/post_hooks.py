"""Auto post-hook chain run after every successful `write_file`.

Chain order (spec § 11): validate -> regenerate SEO -> git commit. The chain
**always completes** all three stages even if earlier ones fail; the report
records which stages succeeded and which produced errors. The orchestrator
(Step 7) is responsible for any persistence (a `validation_result` material)
or alarms it wants to derive from the report — this module performs no DB
writes and no event emissions.

Layering:
  stdlib + subprocess + harness.services.validators only. No store, no tools,
  no orchestrator. Git is invoked via `subprocess.run` (no GitPython).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from harness.services import validators


# Files we generate; never enumerated as pages in the sitemap.
_SEO_FILE_NAMES = frozenset({"sitemap.xml", "robots.txt", "llms.txt"})


@dataclass
class PostHookReport:
    """Aggregated outcome of the validate -> SEO -> git chain.

    `errors` collects any stage-level exceptions captured as strings so the
    chain can keep running. Per-file validation failures are reflected in
    `html_reports`/`css_reports` and `validate_ok`, not in `errors`.
    """

    validate_ok: bool = True
    html_reports: dict[str, dict] = field(default_factory=dict)
    css_reports: dict[str, dict] = field(default_factory=dict)
    seo_regenerated: bool = False
    seo_report: dict = field(default_factory=dict)
    git_commit_sha: str | None = None
    git_message: str = ""
    errors: list[str] = field(default_factory=list)


def run(sandbox_path: Path, *, base_url: str = "http://localhost:8000/") -> PostHookReport:
    """Run the full chain. Always returns; never raises on normal failures.

    Stages:
      1. Validate every *.html and *.css under sandbox.
      2. Regenerate sitemap.xml / robots.txt / llms.txt.
      3. Git commit ("auto: post-hook iteration") — lazy-inits the repo.
    """
    sandbox = Path(sandbox_path)
    report = PostHookReport()

    # Stage 1 — Validate
    try:
        _validate_stage(sandbox, report)
    except Exception as exc:
        report.errors.append(f"validate stage crashed: {type(exc).__name__}: {exc}")
        report.validate_ok = False

    # Stage 2 — Regenerate SEO (independent of stage 1 outcome)
    try:
        _seo_stage(sandbox, base_url, report)
    except Exception as exc:
        report.errors.append(f"seo stage crashed: {type(exc).__name__}: {exc}")
        report.seo_regenerated = False

    # Stage 3 — Git commit (independent of stage 1 & 2 outcomes)
    try:
        _git_stage(sandbox, report)
    except Exception as exc:
        report.errors.append(f"git stage crashed: {type(exc).__name__}: {exc}")
        report.git_commit_sha = None
        if not report.git_message:
            report.git_message = f"crashed: {exc}"

    return report


# ---------------------------------------------------------------------------
# Stage 1 — Validate
# ---------------------------------------------------------------------------


def _validate_stage(sandbox: Path, report: PostHookReport) -> None:
    all_ok = True
    saw_any = False

    for path in sorted(sandbox.rglob("*.html")):
        if not path.is_file() or _is_under_git(path, sandbox):
            continue
        rel = str(path.relative_to(sandbox))
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            report.errors.append(f"read {rel}: {exc}")
            report.html_reports[rel] = {
                "html5_parses": False,
                "html5_errors": [str(exc)],
                "has_title": False,
                "has_meta_viewport": False,
                "has_lang": False,
                "has_h1": False,
            }
            all_ok = False
            saw_any = True
            continue
        html_report = validators.validate_html(content)
        report.html_reports[rel] = html_report
        saw_any = True
        if not html_report["html5_parses"]:
            all_ok = False

    for path in sorted(sandbox.rglob("*.css")):
        if not path.is_file() or _is_under_git(path, sandbox):
            continue
        rel = str(path.relative_to(sandbox))
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            report.errors.append(f"read {rel}: {exc}")
            report.css_reports[rel] = {
                "css_parses": False,
                "css_errors": [str(exc)],
            }
            all_ok = False
            saw_any = True
            continue
        css_report = validators.validate_css(content)
        report.css_reports[rel] = css_report
        saw_any = True
        if not css_report["css_parses"]:
            all_ok = False

    # `validate_ok` is True when every report passed. If no validatable files
    # exist, it is vacuously True — there is nothing to fail on.
    report.validate_ok = all_ok if saw_any else True


def _is_under_git(path: Path, sandbox: Path) -> bool:
    """Skip any file inside the sandbox's .git directory."""
    try:
        rel = path.relative_to(sandbox)
    except ValueError:
        return False
    parts = rel.parts
    return bool(parts) and parts[0] == ".git"


# ---------------------------------------------------------------------------
# Stage 2 — Regenerate SEO artifacts
# ---------------------------------------------------------------------------


def _seo_stage(sandbox: Path, base_url: str, report: PostHookReport) -> None:
    sandbox.mkdir(parents=True, exist_ok=True)

    html_paths = sorted(
        p.relative_to(sandbox)
        for p in sandbox.rglob("*.html")
        if p.is_file()
        and not _is_under_git(p, sandbox)
        and p.name not in _SEO_FILE_NAMES
    )

    sitemap = _build_sitemap(html_paths, base_url)
    robots = _build_robots(base_url)
    llms = _build_llms()

    (sandbox / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    (sandbox / "robots.txt").write_text(robots, encoding="utf-8")
    (sandbox / "llms.txt").write_text(llms, encoding="utf-8")

    seo_report = validators.validate_seo(sandbox)
    report.seo_report = seo_report
    report.seo_regenerated = bool(seo_report["sitemap_xml_valid"])


def _build_sitemap(html_paths: list[Path], base_url: str) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for rel in html_paths:
        loc = base + str(rel).replace("\\", "/")
        lines.append(f"  <url><loc>{_xml_escape(loc)}</loc></url>")
    lines.append("</urlset>")
    lines.append("")
    return "\n".join(lines)


def _build_robots(base_url: str) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    return f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n"


def _build_llms() -> str:
    return "# Site\n\nGenerated by harness v1.\n"


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Stage 3 — Git commit
# ---------------------------------------------------------------------------


def _git_stage(sandbox: Path, report: PostHookReport) -> None:
    sandbox.mkdir(parents=True, exist_ok=True)

    if not (sandbox / ".git").is_dir():
        init = _git(sandbox, ["init", "--quiet"])
        if init.returncode != 0:
            report.errors.append(f"git init: {init.stderr.strip()}")
            report.git_message = "git init failed"
            return
        _git(sandbox, ["config", "user.email", "harness@local"])
        _git(sandbox, ["config", "user.name", "harness"])
        # Avoid signing or hook interference inside the sandbox repo.
        _git(sandbox, ["config", "commit.gpgsign", "false"])

    add = _git(sandbox, ["add", "."])
    if add.returncode != 0:
        report.errors.append(f"git add: {add.stderr.strip()}")
        report.git_message = "git add failed"
        return

    commit = _git(
        sandbox,
        ["commit", "-m", "auto: post-hook iteration"],
    )
    combined = (commit.stdout or "") + (commit.stderr or "")
    if commit.returncode != 0:
        if "nothing to commit" in combined or "no changes added" in combined:
            report.git_commit_sha = None
            report.git_message = "nothing to commit"
            return
        report.errors.append(f"git commit: {combined.strip()}")
        report.git_message = "git commit failed"
        return

    rev = _git(sandbox, ["rev-parse", "HEAD"])
    if rev.returncode != 0:
        report.errors.append(f"git rev-parse: {rev.stderr.strip()}")
        report.git_message = "commit succeeded but rev-parse failed"
        return
    sha = rev.stdout.strip()
    report.git_commit_sha = sha if len(sha) == 40 else None
    report.git_message = "committed"


def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
