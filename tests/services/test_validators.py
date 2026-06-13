"""Step 5 gate — `harness.services.validators` pure functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.services import validators


# ---------------------------------------------------------------------------
# validate_html
# ---------------------------------------------------------------------------


GOOD_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Maria's Trattoria</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
  </head>
  <body>
    <h1>Welcome</h1>
    <p>Hello world.</p>
  </body>
</html>
"""


def test_validate_html_good_passes_all_checks():
    r = validators.validate_html(GOOD_HTML)
    assert r["html5_parses"] is True
    assert r["html5_errors"] == []
    assert r["has_title"] is True
    assert r["has_meta_viewport"] is True
    assert r["has_lang"] is True
    assert r["has_h1"] is True


def test_validate_html_missing_title_flagged():
    html = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width">
  </head>
  <body><h1>Hi</h1></body>
</html>
"""
    r = validators.validate_html(html)
    assert r["html5_parses"] is True
    assert r["has_title"] is False
    assert r["has_meta_viewport"] is True
    assert r["has_lang"] is True
    assert r["has_h1"] is True


def test_validate_html_missing_lang_and_viewport_and_h1():
    html = """<!DOCTYPE html>
<html>
  <head><title>X</title></head>
  <body><p>no h1 here</p></body>
</html>
"""
    r = validators.validate_html(html)
    assert r["html5_parses"] is True
    assert r["has_title"] is True
    assert r["has_lang"] is False
    assert r["has_meta_viewport"] is False
    assert r["has_h1"] is False


def test_validate_html_malformed_flagged_by_strict_mode():
    # Missing DOCTYPE is enough for html5lib strict mode to raise a ParseError.
    bad = "<title>x</title><body><h1>hi</h1></body>"
    r = validators.validate_html(bad)
    assert r["html5_parses"] is False
    assert r["html5_errors"]
    assert any(isinstance(e, str) and e for e in r["html5_errors"])
    # Tolerant tree-walk still gives us tag-presence info.
    assert r["has_title"] is True
    assert r["has_h1"] is True


def test_validate_html_empty_string_is_graceful():
    r = validators.validate_html("")
    # No crash. Strict mode raises on empty input; presence checks all False.
    assert r["html5_parses"] is False
    assert r["has_title"] is False
    assert r["has_meta_viewport"] is False
    assert r["has_lang"] is False
    assert r["has_h1"] is False


def test_validate_html_h1_with_nested_element_counts():
    html = """<!DOCTYPE html>
<html lang="en">
  <head><title>X</title><meta name="viewport" content="x"></head>
  <body><h1><span>Hello</span></h1></body>
</html>
"""
    r = validators.validate_html(html)
    assert r["has_h1"] is True


def test_validate_html_empty_title_does_not_count():
    html = """<!DOCTYPE html>
<html lang="en">
  <head><title>   </title><meta name="viewport" content="x"></head>
  <body><h1>Hi</h1></body>
</html>
"""
    r = validators.validate_html(html)
    assert r["has_title"] is False


# ---------------------------------------------------------------------------
# validate_css
# ---------------------------------------------------------------------------


def test_validate_css_good_passes():
    r = validators.validate_css("body { color: red; }\nh1 { font-size: 2em; }\n")
    assert r["css_parses"] is True
    assert r["css_errors"] == []


def test_validate_css_empty_string_is_valid():
    r = validators.validate_css("")
    assert r["css_parses"] is True
    assert r["css_errors"] == []


def test_validate_css_top_level_parse_error_caught():
    # Stray @ with no rule body produces a ParseError at the top level.
    r = validators.validate_css("@@@invalid")
    assert r["css_parses"] is False
    assert r["css_errors"]
    assert all(isinstance(m, str) and m for m in r["css_errors"])


def test_validate_css_bare_at_token_is_caught():
    # A lone `@` with no name or block produces a top-level ParseError.
    # (tinycss2 is lenient about many truncations — auto-closes blocks at EOF
    # and silently drops malformed declarations — but it does flag this one.)
    r = validators.validate_css("@")
    assert r["css_parses"] is False
    assert r["css_errors"]


# ---------------------------------------------------------------------------
# validate_seo
# ---------------------------------------------------------------------------


VALID_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://localhost:8000/index.html</loc></url>
  <url><loc>http://localhost:8000/about.html</loc></url>
</urlset>
"""


def test_validate_seo_all_present_and_valid(tmp_path: Path):
    (tmp_path / "sitemap.xml").write_text(VALID_SITEMAP, encoding="utf-8")
    (tmp_path / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    (tmp_path / "llms.txt").write_text("# Site\n", encoding="utf-8")

    r = validators.validate_seo(tmp_path)
    assert r["sitemap_xml_valid"] is True
    assert r["sitemap_xml_url_count"] == 2
    assert r["robots_txt_present"] is True
    assert r["llms_txt_present"] is True
    assert r["errors"] == []


def test_validate_seo_missing_sitemap(tmp_path: Path):
    (tmp_path / "robots.txt").write_text("x", encoding="utf-8")
    (tmp_path / "llms.txt").write_text("x", encoding="utf-8")

    r = validators.validate_seo(tmp_path)
    assert r["sitemap_xml_valid"] is False
    assert r["sitemap_xml_url_count"] == 0
    assert r["robots_txt_present"] is True
    assert r["llms_txt_present"] is True
    assert any("sitemap.xml missing" in e for e in r["errors"])


def test_validate_seo_malformed_sitemap(tmp_path: Path):
    (tmp_path / "sitemap.xml").write_text("<urlset><url><loc>oops", encoding="utf-8")
    (tmp_path / "robots.txt").write_text("x", encoding="utf-8")
    (tmp_path / "llms.txt").write_text("x", encoding="utf-8")

    r = validators.validate_seo(tmp_path)
    assert r["sitemap_xml_valid"] is False
    assert any("parse error" in e for e in r["errors"])


def test_validate_seo_wrong_root_tag(tmp_path: Path):
    (tmp_path / "sitemap.xml").write_text(
        '<?xml version="1.0"?><nope/>', encoding="utf-8"
    )
    r = validators.validate_seo(tmp_path)
    assert r["sitemap_xml_valid"] is False
    assert any("urlset" in e for e in r["errors"])


def test_validate_seo_missing_robots_and_llms(tmp_path: Path):
    (tmp_path / "sitemap.xml").write_text(VALID_SITEMAP, encoding="utf-8")
    r = validators.validate_seo(tmp_path)
    assert r["sitemap_xml_valid"] is True
    assert r["sitemap_xml_url_count"] == 2
    assert r["robots_txt_present"] is False
    assert r["llms_txt_present"] is False


@pytest.mark.parametrize("url_count", [0, 1, 5])
def test_validate_seo_url_count_matches(tmp_path: Path, url_count: int):
    urls = "".join(
        f"  <url><loc>http://x/{i}.html</loc></url>\n" for i in range(url_count)
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n"
    )
    (tmp_path / "sitemap.xml").write_text(body, encoding="utf-8")
    r = validators.validate_seo(tmp_path)
    assert r["sitemap_xml_valid"] is True
    assert r["sitemap_xml_url_count"] == url_count
