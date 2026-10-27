"""HTML sanitizing and plain-text extraction for vacancy descriptions."""

from __future__ import annotations

import re

import nh3
from bs4 import BeautifulSoup

from app.services.resume_text import normalize_whitespace

ALLOWED_TAGS = {
    "p",
    "br",
    "ul",
    "ol",
    "li",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "blockquote",
    "a",
    "span",
    "div",
    "table",
    "thead",
    "tbody",
    "tr",
    "td",
    "th",
    "hr",
    "pre",
    "code",
}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}}
_HIGHLIGHT_RE = re.compile(r"</?highlighttext>", re.IGNORECASE)


def sanitize_html(html: str | None) -> str:
    if not html:
        return ""
    html = _HIGHLIGHT_RE.sub("", html)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer nofollow",
        url_schemes={"http", "https", "mailto"},
        strip_comments=True,
    )


BLOCK_TAGS = [
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "table",
    "blockquote",
    "pre",
    "section",
    "article",
    "header",
    "footer",
    "hr",
]


def html_to_text(html: str | None) -> str:
    """Block elements become line breaks, inline elements are joined without separators."""
    if not html:
        return ""
    html = _HIGHLIGHT_RE.sub("", html)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.append("\n")
    for li in soup.find_all("li"):
        li.insert(0, "• ")
    text = soup.get_text("")
    return normalize_whitespace(text)


def strip_highlight(text: str | None) -> str:
    return _HIGHLIGHT_RE.sub("", text or "")
