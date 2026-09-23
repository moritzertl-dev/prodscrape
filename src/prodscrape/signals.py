"""Stage 2 tier A — deterministic page signals. No model calls.

This is where the token budget is won. Structural evidence classifies the clear-cut
majority of pages for free; only the ambiguous remainder is ever sent to the model, and
then only as a compact digest.

Note the reference site carries **zero JSON-LD** (PIPELINE.md §6), so schema.org
``@type: Product`` is treated as a bonus signal, never a prerequisite.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

SPEC_HEADINGS = (
    # en
    "technical data", "technical specifications", "specifications", "tech specs",
    "specification",
    # de
    "technische daten", "spezifikationen",
    # fr / es / it / pt / nl / pl
    "données techniques", "caractéristiques techniques", "spécifications",
    "datos técnicos", "especificaciones", "características técnicas",
    "dati tecnici", "specifiche tecniche",
    "dados técnicos", "especificações",
    "technische gegevens", "specificaties",
    "dane techniczne", "specyfikacja",
    # zh / ja
    "技术参数", "技术数据", "規格", "仕様",
)
ORDER_HEADINGS = (
    "order information", "ordering information", "ordering", "order data",
    "bestellinformationen", "bestelldaten",
    "informations de commande", "información de pedido", "informazioni per l'ordine",
    "bestelinformatie",
)
# Matched against a whole heading, not as a substring: product pages legitimately carry
# widgets like "Publication Finder", and substring matching turned that into a 0.5 penalty
# on a real instrument page.
NEGATIVE_HEADINGS = (
    "job description",
    "press release",
    "press releases",
    "webinar",
    "webinars",
    "publications",
    "newsletter",
)

# A catalogue/order code, generically: an alphanumeric token containing at least one
# digit and split by at least two separators.
#
# This used to be `\d{3}-\d{4,6}-\d`, which is Analytik Jena's format and nobody else's —
# every other vendor silently scored 0.2 lower on a signal they could never earn. A
# vendor with a genuinely different shape can still override it in its recipe.
ORDER_NUMBER_RE = re.compile(
    r"\b(?=[A-Z0-9]*\d)[A-Z0-9]{2,}[-./][A-Z0-9]{2,}[-./][A-Z0-9]{1,}\b", re.I
)

# Dates and timestamps have exactly the shape of an order code. Left unfiltered they
# handed a press release the same "has order numbers" signal as a product page.
_DATE_LIKE_RE = re.compile(
    r"^(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}"      # 2024-03-31
    r"|\d{1,2}[-./]\d{1,2}[-./]\d{2,4})",      # 31.03.2024
    re.I,
)


def looks_like_date(token: str) -> bool:
    return bool(_DATE_LIKE_RE.match(token.strip()))


def find_order_numbers(text: str, limit: int = 20) -> list[str]:
    """Catalogue codes in a blob of text, with dates and timestamps excluded."""
    return sorted(
        {m for m in ORDER_NUMBER_RE.findall(text) if not looks_like_date(m)}
    )[:limit]


DATASHEET_RE = re.compile(r"\.pdf(\?|$)", re.I)


@dataclass
class PageSignals:
    url: str
    depth: int
    slug: str
    title: str = ""
    h1: str = ""
    headings: list[str] = field(default_factory=list)
    table_count: int = 0
    has_jsonld_product: bool = False
    has_spec_heading: bool = False
    has_order_heading: bool = False
    order_numbers: list[str] = field(default_factory=list)
    pdf_links: int = 0
    text_length: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _headings(tree: HTMLParser) -> list[str]:
    out = []
    for level in ("h1", "h2", "h3"):
        for node in tree.css(level):
            text = " ".join(node.text(separator=" ", strip=True).split())
            if text:
                out.append(text)
    return out


def _jsonld_has_product(tree: HTMLParser) -> bool:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, TypeError):
            continue
        blobs = data if isinstance(data, list) else [data]
        for blob in blobs:
            if isinstance(blob, dict) and "product" in str(blob.get("@type", "")).lower():
                return True
    return False


def page_signals(
    url: str,
    html: str,
    *,
    spec_headings: tuple[str, ...] | list[str] | None = None,
    order_headings: tuple[str, ...] | list[str] | None = None,
) -> PageSignals:
    """Extract structural signals.

    Heading vocabularies are overridable so a site recipe can add vendor-specific wording
    (BINDER says "Technical specifications", analytik-jena says "Technical Data"). Recipe
    terms are *added* to the defaults rather than replacing them, so a partial recipe can
    never make classification worse than the built-in behaviour.
    """
    spec_terms = tuple(SPEC_HEADINGS) + tuple(h.lower() for h in (spec_headings or ()))
    order_terms = tuple(ORDER_HEADINGS) + tuple(h.lower() for h in (order_headings or ()))
    tree = HTMLParser(html)
    segments = [s for s in urlparse(url).path.split("/") if s]
    heads = _headings(tree)
    lowered = [h.lower() for h in heads]
    body_text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    title_node = tree.css_first("title")
    h1_node = tree.css_first("h1")

    return PageSignals(
        url=url,
        depth=len(segments),
        slug=segments[-1] if segments else "",
        title=title_node.text(strip=True) if title_node else "",
        h1=" ".join(h1_node.text(separator=" ", strip=True).split()) if h1_node else "",
        headings=heads,
        table_count=len(tree.css("table")),
        has_jsonld_product=_jsonld_has_product(tree),
        has_spec_heading=any(k in h for h in lowered for k in spec_terms),
        has_order_heading=any(k in h for h in lowered for k in order_terms),
        order_numbers=find_order_numbers(body_text),
        pdf_links=sum(
            1
            for a in tree.css("a[href]")
            if DATASHEET_RE.search(a.attributes.get("href") or "")
        ),
        text_length=len(body_text),
    )


@dataclass
class Verdict:
    url: str
    label: str
    confidence: float
    reason: str
    decided_by: str


def classify_by_signals(
    sig: PageSignals,
    *,
    family_depth: int | None = None,
    slug_suffix: str | None = None,
    threshold: float = 0.6,
    prior: float = 0.0,
    prior_reason: str = "",
) -> Verdict:
    """Score a page from structural evidence alone.

    Returns label ``"unknown"`` when the evidence is thin — those pages, and only those,
    go to the model in tier B.
    """
    score = 0.0
    reasons: list[str] = []

    # How the page was reached is evidence too: a page the scope step named as an
    # instrument from the vendor's own menu starts ahead of a link found on a hub.
    if prior:
        score += prior
        reasons.append(prior_reason or f"prior {prior:+.2f}")
    if sig.has_spec_heading:
        score += 0.45
        reasons.append("has a technical-data heading")
    if sig.has_order_heading:
        score += 0.2
        reasons.append("has an order-information heading")
    if sig.order_numbers:
        score += 0.2
        reasons.append(f"{len(sig.order_numbers)} order numbers present")
    if sig.has_jsonld_product:
        score += 0.3
        reasons.append("JSON-LD Product")
    if sig.pdf_links:
        score += 0.1
        reasons.append(f"{sig.pdf_links} PDF links")
    if family_depth is not None and sig.depth == family_depth:
        score += 0.15
        reasons.append(f"URL at family depth {family_depth}")
    if slug_suffix and sig.slug.endswith(slug_suffix):
        score += 0.15
        reasons.append(f"slug ends with {slug_suffix!r}")

    negative = any(
        h.strip().lower().rstrip(":") in NEGATIVE_HEADINGS for h in sig.headings
    )
    if negative:
        score -= 0.5
        reasons.append("editorial/news heading present")

    score = max(0.0, min(score, 1.0))

    # A page only gets *dropped* on negative evidence or a total absence of signals.
    # Weak-but-positive evidence escalates to the model instead.
    #
    # Rationale: a false positive pollutes the final table, a false negative only costs a
    # second look. Dropping at score <= 0.2 silently discarded real products on
    # qinstruments.com (BioShake Q2, Q1 3.0 mm, D30-T) — thin pages whose only signal was
    # URL depth. Escalating them costs a few tokens; dropping them costs recall no later
    # stage can recover.
    #
    # Negative evidence alone never outweighs positive evidence either: Formulatrix's
    # Mantis, Tempest and NT8 pages carry a "Publications" section, and treating that
    # as decisive dropped nine real instruments whose pages also listed 20 order codes.
    # Mixed evidence is exactly what the judgment tier is for.
    positive = any((
        sig.has_spec_heading, sig.has_order_heading, sig.order_numbers,
        sig.has_jsonld_product, prior > 0,
    ))
    if score >= threshold:
        label = "instrument"
    elif score == 0.0 and not positive:
        label = "other"
    elif negative and not positive:
        label = "other"
    else:
        label = "unknown"

    return Verdict(
        url=sig.url,
        label=label,
        confidence=score,
        reason="; ".join(reasons) or "no structural signals",
        decided_by="signals",
    )
