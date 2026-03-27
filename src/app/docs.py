"""Markdown-backed documentation content system."""

import re
from pathlib import Path

import yaml
from markdown import Markdown

DOCS_DIR = Path(__file__).parent.parent / "content" / "docs"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter dict, body string)."""
    text = text.strip()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
            return fm, body
        if len(parts) == 2:
            fm = yaml.safe_load(parts[1]) or {}
            return fm, ""
    return {}, text


def _load_doc_page(filename: str) -> dict:
    """Load a documentation page from a markdown file."""
    filepath = DOCS_DIR / filename
    content = filepath.read_text()
    frontmatter, body = _parse_frontmatter(content)

    return {
        "title": frontmatter.get("title", ""),
        "description": frontmatter.get("description", ""),
        "order": frontmatter.get("order", 999),
        "body": body,
    }


# Slug is derived from the registry key, not from frontmatter
DOCS_PAGES: dict[str, dict] = {
    "overview": _load_doc_page("overview.md"),
    "agents": _load_doc_page("agents.md"),
    "best-practices": _load_doc_page("best-practices.md"),
    "pricing": _load_doc_page("pricing.md"),
    "privacy-security": _load_doc_page("privacy-security.md"),
    "deployment": _load_doc_page("deployment.md"),
    "faq": _load_doc_page("faq.md"),
}


def _sanitize_html(html: str) -> str:
    """Remove dangerous URL schemes from HTML to prevent XSS."""
    # Pattern matches href/src attributes with dangerous schemes
    dangerous_scheme_pattern = re.compile(
        r'(href|src)\s*=\s*["\']?\s*(javascript|vbscript|data|file)\s*:', re.IGNORECASE
    )
    return dangerous_scheme_pattern.sub(r'\1="#"', html)


def render_doc_page(content: str) -> str:
    """Render markdown content to HTML with HTML escaping for security."""
    _, body = _parse_frontmatter(content)

    # Escape HTML before markdown conversion to prevent XSS
    escaped = body.replace("&", "&amp;")
    escaped = escaped.replace("<", "&lt;")
    escaped = escaped.replace(">", "&gt;")

    md = Markdown(extensions=[])
    html = md.convert(escaped)

    # Sanitize dangerous URL schemes after markdown conversion
    return _sanitize_html(html)
