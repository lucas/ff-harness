"""Step 6 gate — `harness.services.checkpoints` pure evaluators + registry.

For each of the 5 evaluators we assert:
  - Pass case: a hand-built good fixture yields `status='pass'` and the
    `criteria_results` keys exactly match the spec § 8 dict (no drift).
  - Fail cases: each individual criterion can be flipped to False (or, for
    list criteria, to non-empty) and we see `status='fail'` with the
    offending criterion reflected.

Plus a registry test asserting exactly the 5 named entries map to callables
that return `CheckpointResult` instances.
"""

from __future__ import annotations

import pytest

from harness.models.enums import CheckpointName, CheckpointStatus
from harness.services import checkpoints
from harness.services.checkpoints import CheckpointResult


# ---------------------------------------------------------------------------
# business_brief_confirmed
# ---------------------------------------------------------------------------


def _brief_material() -> dict:
    return {
        "id": "m1",
        "type": "business_brief",
        "direction": "out",
        "stage": "bootstrap",
        "content": {
            "industry": "restaurant",
            "name": "Maria's Trattoria",
            "contact": "hello@example.com",
            "pages": ["home", "menu", "contact"],
            "palette": ["#c0392b", "#f5cba7"],
        },
    }


def _approval_material(approved: bool = True) -> dict:
    return {
        "id": "m2",
        "type": "user_approval",
        "direction": "in",
        "stage": "bootstrap",
        "content": {"approved": approved, "notes": "looks great"},
    }


def test_business_brief_confirmed_pass():
    r = checkpoints.evaluate_business_brief_confirmed(
        brief_material=_brief_material(),
        approval_material=_approval_material(approved=True),
    )
    assert isinstance(r, CheckpointResult)
    assert r.name == CheckpointName.BUSINESS_BRIEF_CONFIRMED.value
    assert r.status == CheckpointStatus.PASS.value
    assert set(r.criteria_results.keys()) == {"brief_persisted", "user_approved"}
    assert r.criteria_results["brief_persisted"] is True
    assert r.criteria_results["user_approved"] is True


def test_business_brief_confirmed_fail_when_brief_missing():
    r = checkpoints.evaluate_business_brief_confirmed(
        brief_material=None,
        approval_material=_approval_material(approved=True),
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["brief_persisted"] is False
    assert r.criteria_results["user_approved"] is True


def test_business_brief_confirmed_fail_when_not_approved():
    r = checkpoints.evaluate_business_brief_confirmed(
        brief_material=_brief_material(),
        approval_material=_approval_material(approved=False),
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["brief_persisted"] is True
    assert r.criteria_results["user_approved"] is False


def test_business_brief_confirmed_fail_when_approval_missing():
    r = checkpoints.evaluate_business_brief_confirmed(
        brief_material=_brief_material(),
        approval_material=None,
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["user_approved"] is False


# ---------------------------------------------------------------------------
# mockup_renders
# ---------------------------------------------------------------------------


def _mockup_material(ascii_text: str, regions: list[str]) -> dict:
    return {
        "id": "m3",
        "type": "mockup",
        "direction": "out",
        "stage": "mockup",
        "content": {"ascii": ascii_text, "regions": regions},
    }


def test_mockup_renders_pass():
    declared = ["header", "hero", "menu", "footer"]
    mat = _mockup_material(
        ascii_text="+---HEADER---+\n|   HERO    |\n|   MENU    |\n+---FOOTER--+",
        regions=["header", "hero", "menu", "footer"],
    )
    r = checkpoints.evaluate_mockup_renders(
        mockup_material=mat,
        declared_sections=declared,
    )
    assert r.name == CheckpointName.MOCKUP_RENDERS.value
    assert r.status == CheckpointStatus.PASS.value
    assert set(r.criteria_results.keys()) == {
        "ascii_non_empty",
        "all_regions_present",
        "declared_sections_covered",
    }
    assert r.criteria_results["ascii_non_empty"] is True
    assert r.criteria_results["all_regions_present"] is True
    assert r.criteria_results["declared_sections_covered"] is True


def test_mockup_renders_fail_when_ascii_empty():
    declared = ["header", "hero"]
    mat = _mockup_material(ascii_text="   \n  ", regions=["header", "hero"])
    r = checkpoints.evaluate_mockup_renders(
        mockup_material=mat,
        declared_sections=declared,
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["ascii_non_empty"] is False
    assert r.criteria_results["all_regions_present"] is True


def test_mockup_renders_fail_when_declared_section_missing():
    declared = ["header", "hero", "menu", "footer"]
    mat = _mockup_material(
        ascii_text="+---HEADER---+\n|   HERO    |\n+---FOOTER--+",
        regions=["header", "hero", "footer"],  # "menu" missing
    )
    r = checkpoints.evaluate_mockup_renders(
        mockup_material=mat,
        declared_sections=declared,
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["ascii_non_empty"] is True
    assert r.criteria_results["all_regions_present"] is False
    # Also fewer regions than declared sections.
    assert r.criteria_results["declared_sections_covered"] is False


def test_mockup_renders_fail_when_material_missing():
    r = checkpoints.evaluate_mockup_renders(
        mockup_material=None,
        declared_sections=["header"],
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["ascii_non_empty"] is False


# ---------------------------------------------------------------------------
# mockup_approved
# ---------------------------------------------------------------------------


def test_mockup_approved_pass():
    r = checkpoints.evaluate_mockup_approved(
        approval_material=_approval_material(approved=True),
    )
    assert r.name == CheckpointName.MOCKUP_APPROVED.value
    assert r.status == CheckpointStatus.PASS.value
    assert set(r.criteria_results.keys()) == {"user_approved"}
    assert r.criteria_results["user_approved"] is True


def test_mockup_approved_fail_when_not_approved():
    r = checkpoints.evaluate_mockup_approved(
        approval_material=_approval_material(approved=False),
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["user_approved"] is False


def test_mockup_approved_fail_when_material_missing():
    r = checkpoints.evaluate_mockup_approved(approval_material=None)
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["user_approved"] is False


# ---------------------------------------------------------------------------
# site_valid
# ---------------------------------------------------------------------------


def _good_html_report() -> dict:
    return {
        "html5_parses": True,
        "html5_errors": [],
        "has_title": True,
        "has_meta_viewport": True,
        "has_lang": True,
        "has_h1": True,
    }


def _good_css_report() -> dict:
    return {"css_parses": True, "css_errors": []}


SITE_VALID_KEYS = {
    "html5_parses",
    "html5_errors",
    "css_parses",
    "css_errors",
    "has_title",
    "has_meta_viewport",
    "has_lang",
    "has_h1",
}


def test_site_valid_pass_with_good_html_and_css():
    r = checkpoints.evaluate_site_valid(
        html_reports={"index.html": _good_html_report()},
        css_reports={"styles.css": _good_css_report()},
    )
    assert r.name == CheckpointName.SITE_VALID.value
    assert r.status == CheckpointStatus.PASS.value
    assert set(r.criteria_results.keys()) == SITE_VALID_KEYS
    assert r.criteria_results["html5_errors"] == []
    assert r.criteria_results["css_errors"] == []
    assert r.criteria_results["has_title"] is True


def test_site_valid_fail_when_html_missing_title():
    bad = _good_html_report()
    bad["has_title"] = False
    r = checkpoints.evaluate_site_valid(
        html_reports={"index.html": bad},
        css_reports={"styles.css": _good_css_report()},
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["has_title"] is False
    # Other criteria still pass.
    assert r.criteria_results["html5_parses"] is True


def test_site_valid_fail_with_html5_parse_errors():
    bad = _good_html_report()
    bad["html5_parses"] = False
    bad["html5_errors"] = ["unexpected EOF", "unclosed <div>"]
    r = checkpoints.evaluate_site_valid(
        html_reports={"index.html": bad},
        css_reports={},
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["html5_parses"] is False
    assert r.criteria_results["html5_errors"] == ["unexpected EOF", "unclosed <div>"]


def test_site_valid_fail_with_empty_html_reports():
    r = checkpoints.evaluate_site_valid(
        html_reports={},
        css_reports={"styles.css": _good_css_report()},
    )
    assert r.status == CheckpointStatus.FAIL.value
    # Spec: empty html_reports -> html5_parses=False and all presence flags=False.
    assert r.criteria_results["html5_parses"] is False
    assert r.criteria_results["has_title"] is False
    assert r.criteria_results["has_meta_viewport"] is False
    assert r.criteria_results["has_lang"] is False
    assert r.criteria_results["has_h1"] is False
    assert r.criteria_results["html5_errors"] == []


def test_site_valid_aggregates_errors_across_multiple_files():
    f1 = _good_html_report()
    f2 = _good_html_report()
    f2["html5_parses"] = False
    f2["html5_errors"] = ["bad tag in file2"]
    r = checkpoints.evaluate_site_valid(
        html_reports={"a.html": f1, "b.html": f2},
        css_reports={},
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["html5_parses"] is False
    assert r.criteria_results["html5_errors"] == ["bad tag in file2"]


def test_site_valid_fail_with_css_parse_error():
    bad_css = {"css_parses": False, "css_errors": ["unexpected }"]}
    r = checkpoints.evaluate_site_valid(
        html_reports={"index.html": _good_html_report()},
        css_reports={"styles.css": bad_css},
    )
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["css_parses"] is False
    assert r.criteria_results["css_errors"] == ["unexpected }"]


# ---------------------------------------------------------------------------
# seo_artifacts_present
# ---------------------------------------------------------------------------


SEO_KEYS = {"sitemap_xml_valid", "robots_txt_present", "llms_txt_present"}


def test_seo_artifacts_present_pass():
    seo_report = {
        "sitemap_xml_valid": True,
        "sitemap_xml_url_count": 3,
        "robots_txt_present": True,
        "llms_txt_present": True,
        "errors": [],
    }
    r = checkpoints.evaluate_seo_artifacts_present(seo_report=seo_report)
    assert r.name == CheckpointName.SEO_ARTIFACTS_PRESENT.value
    assert r.status == CheckpointStatus.PASS.value
    assert set(r.criteria_results.keys()) == SEO_KEYS
    assert r.criteria_results["sitemap_xml_valid"] is True
    assert r.criteria_results["robots_txt_present"] is True
    assert r.criteria_results["llms_txt_present"] is True


def test_seo_artifacts_present_fail_when_sitemap_invalid():
    seo_report = {
        "sitemap_xml_valid": False,
        "sitemap_xml_url_count": 0,
        "robots_txt_present": True,
        "llms_txt_present": True,
        "errors": ["sitemap.xml parse error: bad token"],
    }
    r = checkpoints.evaluate_seo_artifacts_present(seo_report=seo_report)
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["sitemap_xml_valid"] is False
    assert r.criteria_results["robots_txt_present"] is True


def test_seo_artifacts_present_fail_when_robots_missing():
    seo_report = {
        "sitemap_xml_valid": True,
        "sitemap_xml_url_count": 1,
        "robots_txt_present": False,
        "llms_txt_present": True,
        "errors": [],
    }
    r = checkpoints.evaluate_seo_artifacts_present(seo_report=seo_report)
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["robots_txt_present"] is False


def test_seo_artifacts_present_fail_when_llms_missing():
    seo_report = {
        "sitemap_xml_valid": True,
        "sitemap_xml_url_count": 1,
        "robots_txt_present": True,
        "llms_txt_present": False,
        "errors": [],
    }
    r = checkpoints.evaluate_seo_artifacts_present(seo_report=seo_report)
    assert r.status == CheckpointStatus.FAIL.value
    assert r.criteria_results["llms_txt_present"] is False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_exactly_the_five_named_checkpoints():
    expected = {
        CheckpointName.BUSINESS_BRIEF_CONFIRMED.value,
        CheckpointName.MOCKUP_RENDERS.value,
        CheckpointName.MOCKUP_APPROVED.value,
        CheckpointName.SITE_VALID.value,
        CheckpointName.SEO_ARTIFACTS_PRESENT.value,
    }
    assert set(checkpoints.REGISTRY.keys()) == expected
    for fn in checkpoints.REGISTRY.values():
        assert callable(fn)


@pytest.mark.parametrize(
    "name,kwargs",
    [
        (
            CheckpointName.BUSINESS_BRIEF_CONFIRMED.value,
            {
                "brief_material": _brief_material(),
                "approval_material": _approval_material(approved=True),
            },
        ),
        (
            CheckpointName.MOCKUP_RENDERS.value,
            {
                "mockup_material": _mockup_material(
                    ascii_text="+--HEAD--+", regions=["header"]
                ),
                "declared_sections": ["header"],
            },
        ),
        (
            CheckpointName.MOCKUP_APPROVED.value,
            {"approval_material": _approval_material(approved=True)},
        ),
        (
            CheckpointName.SITE_VALID.value,
            {
                "html_reports": {"index.html": _good_html_report()},
                "css_reports": {"styles.css": _good_css_report()},
            },
        ),
        (
            CheckpointName.SEO_ARTIFACTS_PRESENT.value,
            {
                "seo_report": {
                    "sitemap_xml_valid": True,
                    "sitemap_xml_url_count": 1,
                    "robots_txt_present": True,
                    "llms_txt_present": True,
                    "errors": [],
                }
            },
        ),
    ],
)
def test_registry_dispatch_returns_checkpoint_result(name, kwargs):
    fn = checkpoints.REGISTRY[name]
    result = fn(**kwargs)
    assert isinstance(result, CheckpointResult)
    assert result.name == name
    assert result.status == CheckpointStatus.PASS.value
