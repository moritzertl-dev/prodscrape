"""Stage 2 tier B — compact page digests.

The agent must never be handed a page. A PlasmaQuant product page is 520 KB; its digest
is a few hundred tokens and carries exactly the evidence a is-this-a-device judgment
needs: what the page is called, where it sits, what its sections are, and whether it has
the structural marks of a product.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from .signals import PageSignals, page_signals
from .tables import to_spec_table
from .extract import find_spec_table

MAX_INTRO_CHARS = 700
MAX_HEADINGS = 12


def _intro_text(tree: HTMLParser, limit: int = MAX_INTRO_CHARS) -> str:
    for node in tree.css("p"):
        text = " ".join(node.text(separator=" ", strip=True).split())
        if len(text) > 80:
            return text[:limit]
    body = tree.body
    text = " ".join(body.text(separator=" ", strip=True).split()) if body else ""
    return text[:limit]


def _breadcrumb(url: str) -> list[str]:
    return [s for s in urlparse(url).path.split("/") if s]


def page_digest(url: str, html: str, signals: PageSignals | None = None) -> dict:
    """A few hundred tokens of evidence, never the page itself."""
    tree = HTMLParser(html)
    sig = signals or page_signals(url, html)

    grid, provenance = find_spec_table(html)
    spec_preview: dict = {}
    if grid is not None:
        table = to_spec_table(grid)
        spec_preview = {
            "source": provenance,
            "variants": table.entities[:6],
            "attributes": table.attributes[:15],
        }

    return {
        "url": url,
        "breadcrumb": _breadcrumb(url),
        "title": sig.title[:160],
        "h1": sig.h1[:160],
        "headings": [h[:80] for h in sig.headings[:MAX_HEADINGS]],
        "intro": _intro_text(tree),
        "has_spec_heading": sig.has_spec_heading,
        "has_order_heading": sig.has_order_heading,
        "order_numbers": sig.order_numbers[:3],
        "table_count": sig.table_count,
        "pdf_links": sig.pdf_links,
        "spec_table": spec_preview,
    }


def digest_size_estimate(digest: dict) -> int:
    """Rough token count, so the cost of a batch is visible before it is spent."""
    return len(re.findall(r"\S+", str(digest)))
