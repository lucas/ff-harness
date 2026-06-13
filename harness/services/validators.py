"""Pure validation functions for HTML, CSS, and SEO artifacts.

All three functions return typed `dict` reports. They never raise on normal
validation failures — errors are returned as data (`*_errors` lists). They
raise only on programming bugs (e.g. wrong argument types). They do no I/O
beyond reading the paths passed to `validate_seo`; they do not log, persist,
or talk to the store.

Reused by:
  - `harness.services.post_hooks.run` (Step 5)
  - `harness.services.checkpoints` for `site_valid` and `seo_artifacts_present`
    (Step 6)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import html5lib
import html5lib.html5parser
import tinycss2

# html5lib emits namespaced tag names like "{http://www.w3.org/1999/xhtml}title".
_XHTML_NS = "{http://www.w3.org/1999/xhtml}"


def _local(tag: str) -> str:
    """Strip an XHTML namespace prefix if present."""
    if tag.startswith(_XHTML_NS):
        return tag[len(_XHTML_NS) :]
    return tag


def validate_html(content: str) -> dict:
    """Validate an HTML string.

    Returns a dict with keys:
      - html5_parses: bool — strict html5lib parse succeeded
      - html5_errors: list[str] — strict-mode error messages
      - has_title: bool — at least one <title> with non-empty text
      - has_meta_viewport: bool — <meta name='viewport' ...> present
      - has_lang: bool — <html lang='...'> with non-empty value
      - has_h1: bool — at least one <h1> with non-empty text
    """
    html5_errors: list[str] = []
    html5_parses = True
    try:
        strict = html5lib.HTMLParser(strict=True)
        strict.parse(content)
    except html5lib.html5parser.ParseError as exc:
        html5_parses = False
        html5_errors.append(str(exc))
    except Exception as exc:  # pragma: no cover — defensive
        html5_parses = False
        html5_errors.append(f"{type(exc).__name__}: {exc}")

    # Non-strict parse for tag-presence walk. html5lib's etree treebuilder
    # returns the root <html> Element directly (no ElementTree wrapper).
    has_title = False
    has_meta_viewport = False
    has_lang = False
    has_h1 = False

    try:
        tolerant = html5lib.HTMLParser(
            tree=html5lib.treebuilders.getTreeBuilder("etree"),
            strict=False,
        )
        root = tolerant.parse(content)
    except Exception:
        # If even tolerant parse fails, leave all presence flags False.
        root = None

    if root is not None:
        for el in root.iter():
            local = _local(el.tag)
            if local == "html":
                lang = el.attrib.get("lang") or el.attrib.get("xml:lang")
                if lang and lang.strip():
                    has_lang = True
            elif local == "title":
                if el.text and el.text.strip():
                    has_title = True
            elif local == "meta":
                name = el.attrib.get("name", "")
                if name.lower() == "viewport":
                    has_meta_viewport = True
            elif local == "h1":
                # An <h1> is non-empty if it has direct text or any child text.
                if (el.text and el.text.strip()) or any(
                    (child.text and child.text.strip())
                    or (child.tail and child.tail.strip())
                    for child in el.iter()
                    if child is not el
                ):
                    has_h1 = True

    return {
        "html5_parses": html5_parses,
        "html5_errors": html5_errors,
        "has_title": has_title,
        "has_meta_viewport": has_meta_viewport,
        "has_lang": has_lang,
        "has_h1": has_h1,
    }


def validate_css(content: str) -> dict:
    """Validate a CSS string.

    Returns:
      - css_parses: bool — no ParseError tokens at any depth
      - css_errors: list[str] — collected ParseError messages
    """
    css_errors: list[str] = []

    def _walk(nodes) -> None:
        for node in nodes or ():
            name = type(node).__name__
            if name == "ParseError":
                msg = getattr(node, "message", "") or str(node)
                css_errors.append(msg)
                continue
            # AtRule and QualifiedRule have prelude + content blocks; both
            # may contain ParseError tokens introduced by malformed inputs.
            prelude = getattr(node, "prelude", None)
            if prelude:
                _walk(prelude)
            inner = getattr(node, "content", None)
            if inner:
                _walk(inner)

    rules = tinycss2.parse_stylesheet(content)
    _walk(rules)
    return {
        "css_parses": len(css_errors) == 0,
        "css_errors": css_errors,
    }


def validate_seo(sandbox_path: Path) -> dict:
    """Validate the three SEO artifacts under a sandbox directory.

    Returns:
      - sitemap_xml_valid: bool — sitemap.xml exists, parses, has urlset root
      - sitemap_xml_url_count: int — count of <url> children when valid
      - robots_txt_present: bool
      - llms_txt_present: bool
      - errors: list[str] — parse errors (only sitemap can produce one in v1)
    """
    errors: list[str] = []
    sandbox = Path(sandbox_path)

    sitemap_path = sandbox / "sitemap.xml"
    robots_path = sandbox / "robots.txt"
    llms_path = sandbox / "llms.txt"

    sitemap_valid = False
    url_count = 0
    if sitemap_path.is_file():
        try:
            tree = ET.parse(sitemap_path)
            root = tree.getroot()
            local = _local_xml(root.tag)
            if local == "urlset":
                sitemap_valid = True
                for child in root:
                    if _local_xml(child.tag) == "url":
                        url_count += 1
            else:
                errors.append(
                    f"sitemap.xml root tag is {local!r}, expected 'urlset'"
                )
        except ET.ParseError as exc:
            errors.append(f"sitemap.xml parse error: {exc}")
    else:
        errors.append("sitemap.xml missing")

    return {
        "sitemap_xml_valid": sitemap_valid,
        "sitemap_xml_url_count": url_count,
        "robots_txt_present": robots_path.is_file(),
        "llms_txt_present": llms_path.is_file(),
        "errors": errors,
    }


def _local_xml(tag: str) -> str:
    """Strip any `{namespace}` prefix from an ElementTree tag."""
    if tag.startswith("{"):
        end = tag.find("}")
        if end != -1:
            return tag[end + 1 :]
    return tag
