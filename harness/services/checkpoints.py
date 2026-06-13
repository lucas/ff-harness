"""Deterministic checkpoint evaluators (spec § 8).

Five pure functions, one per named checkpoint, plus a `REGISTRY` mapping the
on-disk name string (matching `CheckpointName` values) to its evaluator. The
orchestrator calls these after the relevant trigger events (Integration Point
#9): post-hook completed -> `site_valid` + `seo_artifacts_present`; user
approval received -> `*_confirmed` / `*_approved`; render_mockup returned ->
`mockup_renders`.

Layering: stdlib + `harness.models.*` only. This module does NOT import
`validators` or `post_hooks` — the orchestrator runs those and hands the
resulting reports in as plain dicts. No DB access. No file I/O. No exceptions
on normal failure: a bad fixture yields `status='fail'`.

Pass rule (spec § 8): `status='pass'` iff every criterion is `True`. For list
criteria (e.g. `html5_errors`), the list must be **empty** for the criterion
to count as "true." `_all_true` enforces both rules uniformly so each
evaluator only has to build its `criteria_results` dict.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.models.enums import CheckpointName, CheckpointStatus


@dataclass
class CheckpointResult:
    """Outcome of one checkpoint evaluation.

    `name` is one of the `CheckpointName` string values. `status` is `'pass'`
    or `'fail'` (the `CheckpointStatus` string values). `criteria_results`
    matches the spec's per-checkpoint criteria dict exactly — keys and types
    are fixed so the persisted JSON is queryable.
    """

    name: str
    status: str
    criteria_results: dict[str, Any] = field(default_factory=dict)


def _all_true(criteria: dict[str, Any]) -> bool:
    """Status='pass' iff every criterion is truthy AND every list value is empty.

    A list criterion (e.g. `html5_errors`) represents collected failure
    messages; it counts as "true" only when empty. All other values use plain
    truthiness (bools, non-zero ints, non-empty strings, etc.).
    """
    for value in criteria.values():
        if isinstance(value, list):
            if value:
                return False
        elif not value:
            return False
    return True


def _status_for(criteria: dict[str, Any]) -> str:
    return (
        CheckpointStatus.PASS.value
        if _all_true(criteria)
        else CheckpointStatus.FAIL.value
    )


# ---------------------------------------------------------------------------
# business_brief_confirmed
# ---------------------------------------------------------------------------


def evaluate_business_brief_confirmed(
    *,
    brief_material: dict | None,
    approval_material: dict | None,
) -> CheckpointResult:
    """Checkpoint fired when a `user_approval` material arrives for the brief.

    `brief_material` is the persisted `business_brief` material row (or None
    if not yet persisted). `approval_material` is the `user_approval` row
    submitted by the human (or None if not yet received). Both are expected
    in their store-row shape: `{..., 'content': {...}, ...}`.

    Criteria (per spec § 8):
      - `brief_persisted`: brief material exists and its `content` is a dict
      - `user_approved`:   approval material exists and `content['approved']`
                           is truthy
    """
    brief_persisted = (
        brief_material is not None
        and isinstance(brief_material.get("content"), dict)
        and bool(brief_material["content"])
    )

    user_approved = False
    if approval_material is not None:
        content = approval_material.get("content")
        if isinstance(content, dict):
            user_approved = bool(content.get("approved"))

    criteria = {
        "brief_persisted": brief_persisted,
        "user_approved": user_approved,
    }
    return CheckpointResult(
        name=CheckpointName.BUSINESS_BRIEF_CONFIRMED.value,
        status=_status_for(criteria),
        criteria_results=criteria,
    )


# ---------------------------------------------------------------------------
# mockup_renders
# ---------------------------------------------------------------------------


def evaluate_mockup_renders(
    *,
    mockup_material: dict | None,
    declared_sections: list[str],
) -> CheckpointResult:
    """Checkpoint fired when `render_mockup` returns.

    `mockup_material` is the persisted `mockup` material row whose
    `content` shape is `{ascii: str, regions: list[str]}`. `declared_sections`
    is the list of section names the worker passed in `layout_spec.sections`.

    Criteria (per spec § 8):
      - `ascii_non_empty`:           ASCII string is non-empty after stripping
      - `all_regions_present`:       every declared section appears in regions
      - `declared_sections_covered`: len(regions) >= len(declared_sections)
    """
    ascii_text = ""
    regions: list[str] = []
    if mockup_material is not None:
        content = mockup_material.get("content")
        if isinstance(content, dict):
            raw_ascii = content.get("ascii")
            if isinstance(raw_ascii, str):
                ascii_text = raw_ascii
            raw_regions = content.get("regions")
            if isinstance(raw_regions, list):
                regions = [str(r) for r in raw_regions]

    ascii_non_empty = bool(ascii_text.strip())
    all_regions_present = all(section in regions for section in declared_sections)
    declared_sections_covered = len(regions) >= len(declared_sections)

    criteria = {
        "ascii_non_empty": ascii_non_empty,
        "all_regions_present": all_regions_present,
        "declared_sections_covered": declared_sections_covered,
    }
    return CheckpointResult(
        name=CheckpointName.MOCKUP_RENDERS.value,
        status=_status_for(criteria),
        criteria_results=criteria,
    )


# ---------------------------------------------------------------------------
# mockup_approved
# ---------------------------------------------------------------------------


def evaluate_mockup_approved(
    *,
    approval_material: dict | None,
) -> CheckpointResult:
    """Checkpoint fired when a `user_approval` material arrives for the mockup.

    Criteria (per spec § 8):
      - `user_approved`: approval material exists and `content['approved']`
                         is truthy
    """
    user_approved = False
    if approval_material is not None:
        content = approval_material.get("content")
        if isinstance(content, dict):
            user_approved = bool(content.get("approved"))

    criteria = {"user_approved": user_approved}
    return CheckpointResult(
        name=CheckpointName.MOCKUP_APPROVED.value,
        status=_status_for(criteria),
        criteria_results=criteria,
    )


# ---------------------------------------------------------------------------
# site_valid
# ---------------------------------------------------------------------------


def evaluate_site_valid(
    *,
    html_reports: dict[str, dict],
    css_reports: dict[str, dict],
) -> CheckpointResult:
    """Checkpoint fired when post-hook validation completes.

    `html_reports` maps each HTML file's sandbox-relative path to its
    `validators.validate_html` report; `css_reports` does the same for CSS
    files. The evaluator aggregates across all files.

    Criteria (per spec § 8):
      - `html5_parses`:     every HTML report has `html5_parses=True`
      - `html5_errors`:     union of every HTML report's `html5_errors`
      - `css_parses`:       every CSS report has `css_parses=True`
      - `css_errors`:       union of every CSS report's `css_errors`
      - `has_title`:        every HTML report has `has_title=True`
      - `has_meta_viewport`: every HTML report has `has_meta_viewport=True`
      - `has_lang`:         every HTML report has `has_lang=True`
      - `has_h1`:           every HTML report has `has_h1=True`

    With no HTML files at all, the per-file presence checks are vacuously
    False (`all([])` is True, but the spec requires "we can't pass site_valid
    with no HTML"). `html5_parses` is likewise False on an empty input set.
    CSS-only is fine for `css_parses` (only relevant when CSS files exist).
    """
    has_html = bool(html_reports)
    has_css = bool(css_reports)

    html5_errors: list[str] = []
    css_errors: list[str] = []

    if has_html:
        html5_parses = all(
            bool(r.get("html5_parses")) for r in html_reports.values()
        )
        has_title = all(bool(r.get("has_title")) for r in html_reports.values())
        has_meta_viewport = all(
            bool(r.get("has_meta_viewport")) for r in html_reports.values()
        )
        has_lang = all(bool(r.get("has_lang")) for r in html_reports.values())
        has_h1 = all(bool(r.get("has_h1")) for r in html_reports.values())
        for r in html_reports.values():
            errs = r.get("html5_errors") or []
            if isinstance(errs, list):
                html5_errors.extend(str(e) for e in errs)
    else:
        # No HTML files -> cannot pass site_valid.
        html5_parses = False
        has_title = False
        has_meta_viewport = False
        has_lang = False
        has_h1 = False

    if has_css:
        css_parses = all(bool(r.get("css_parses")) for r in css_reports.values())
        for r in css_reports.values():
            errs = r.get("css_errors") or []
            if isinstance(errs, list):
                css_errors.extend(str(e) for e in errs)
    else:
        # No CSS is acceptable; treat as a vacuous pass.
        css_parses = True

    criteria = {
        "html5_parses": html5_parses,
        "html5_errors": html5_errors,
        "css_parses": css_parses,
        "css_errors": css_errors,
        "has_title": has_title,
        "has_meta_viewport": has_meta_viewport,
        "has_lang": has_lang,
        "has_h1": has_h1,
    }
    return CheckpointResult(
        name=CheckpointName.SITE_VALID.value,
        status=_status_for(criteria),
        criteria_results=criteria,
    )


# ---------------------------------------------------------------------------
# seo_artifacts_present
# ---------------------------------------------------------------------------


def evaluate_seo_artifacts_present(
    *,
    seo_report: dict,
) -> CheckpointResult:
    """Checkpoint fired when post-hook SEO regen completes.

    `seo_report` is the dict returned by `validators.validate_seo`. The
    three booleans are lifted directly.

    Criteria (per spec § 8):
      - `sitemap_xml_valid`:  sitemap exists, parses, has urlset root
      - `robots_txt_present`: robots.txt is a file under the sandbox
      - `llms_txt_present`:   llms.txt is a file under the sandbox
    """
    criteria = {
        "sitemap_xml_valid": bool(seo_report.get("sitemap_xml_valid")),
        "robots_txt_present": bool(seo_report.get("robots_txt_present")),
        "llms_txt_present": bool(seo_report.get("llms_txt_present")),
    }
    return CheckpointResult(
        name=CheckpointName.SEO_ARTIFACTS_PRESENT.value,
        status=_status_for(criteria),
        criteria_results=criteria,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


REGISTRY: dict[str, Callable[..., CheckpointResult]] = {
    CheckpointName.BUSINESS_BRIEF_CONFIRMED.value: evaluate_business_brief_confirmed,
    CheckpointName.MOCKUP_RENDERS.value: evaluate_mockup_renders,
    CheckpointName.MOCKUP_APPROVED.value: evaluate_mockup_approved,
    CheckpointName.SITE_VALID.value: evaluate_site_valid,
    CheckpointName.SEO_ARTIFACTS_PRESENT.value: evaluate_seo_artifacts_present,
}
