"""Stage 0 — site profiling: robots, sitemap discovery, URL harvesting.

Key lesson from the reference site (PIPELINE.md §6): analytik-jena.com's robots.txt
declares no ``Sitemap:`` directive, yet ``/sitemap.xml`` serves a valid sitemap index.
Probing default paths is therefore mandatory, not a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from .fetch import Fetcher

SITEMAP_PROBE_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap.xml.gz",
    "/wp-sitemap.xml",
    "/sitemap/sitemap-index.xml",
)

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
_SITEMAPINDEX_RE = re.compile(r"<sitemapindex", re.I)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)

PLATFORM_HINTS = {
    "typo3": ("/typo3/", "typo3temp", "/_assets/"),
    "wordpress": ("/wp-content/", "/wp-includes/"),
    "shopify": ("cdn.shopify.com", "/collections/"),
    "drupal": ("/sites/default/files/", "drupal.js"),
    "aem": ("/etc.clientlibs/", "/content/dam/"),
}


@dataclass
class SiteProfile:
    domain: str
    base_url: str
    robots_sitemaps: list[str] = field(default_factory=list)
    sitemaps_found: list[str] = field(default_factory=list)
    platform: str | None = None
    urls: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    crawled: bool = False
    blocked: bool = False

    @property
    def sitemap_declared_in_robots(self) -> bool:
        return bool(self.robots_sitemaps)

    @property
    def robots_ok(self) -> bool:
        """False when robots.txt could not be read at all.

        Distinguishing "no Sitemap: directive" from "robots.txt never loaded" matters:
        the first is a fact about the site, the second is a fault in our run. Reporting
        them identically once sent us hunting for a missing sitemap that was declared
        all along.
        """
        return not any(e.startswith("robots:") for e in self.errors)


def parse_robots_sitemaps(robots_text: str) -> list[str]:
    """Extract ``Sitemap:`` directives. Returns [] when none are declared."""
    out = []
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                out.append(url)
    return out


def parse_sitemap(xml: str) -> tuple[bool, list[dict]]:
    """Parse a sitemap or sitemap index.

    Returns ``(is_index, entries)`` where each entry is ``{"loc":..., "lastmod":...}``.
    Sitemap XML routinely arrives with ``&amp;``-escaped query strings; unescape them or
    the follow-up fetch 404s.
    """
    is_index = bool(_SITEMAPINDEX_RE.search(xml))
    locs = [loc.replace("&amp;", "&") for loc in _LOC_RE.findall(xml)]
    mods = _LASTMOD_RE.findall(xml)
    entries = [
        {"loc": loc, "lastmod": mods[i] if i < len(mods) else None}
        for i, loc in enumerate(locs)
    ]
    return is_index, entries


SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".zip",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".avi",
    ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf", ".eot",
)


def looks_like_sitemap(text: str) -> bool:
    """Whether a response is actually a sitemap.

    A 200 status is not enough: qinstruments.com's robots.txt declares /sitemap.xml,
    which returns 200 with the body "Invalid error handler configuration: t3://page?uid=234".
    Parsing that yields zero URLs and looks indistinguishable from an empty site.
    """
    head = text[:2048].lower()
    return "<urlset" in head or "<sitemapindex" in head or "<loc>" in head


def extract_links(html: str, page_url: str, base_netloc: str) -> list[str]:
    """Internal, crawlable links from a page — same host, no assets, no fragments."""
    out: list[str] = []
    seen: set[str] = set()
    for node in HTMLParser(html).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or parsed.netloc != base_netloc:
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


# Path fragments that suggest a catalogue. Used only to *order* the crawl, never to
# exclude anything — a wrong guess costs ordering, not coverage.
CATALOGUE_HINTS = (
    "product", "produkt", "produit", "producto", "prodotto",
    "shop", "catalog", "katalog", "catalogue",
    "instrument", "geraet", "equipment", "portfolio", "range", "sortiment",
)
EDITORIAL_HINTS = (
    "news", "press", "blog", "career", "job", "event", "story", "about",
    "contact", "legal", "privacy", "imprint", "impressum", "webinar",
    "download", "support", "service", "login", "account", "cart", "search",
)


def crawl_priority(url: str) -> int:
    """Lower sorts first. Catalogue-looking paths are crawled before editorial ones.

    With a bounded crawl the ordering decides what you end up with: a plain FIFO spends
    its budget on whatever the homepage happened to link first, which on most vendor
    sites is news and legal pages.
    """
    path = urlparse(url).path.lower()
    if any(h in path for h in CATALOGUE_HINTS):
        return 0
    if any(h in path for h in EDITORIAL_HINTS):
        return 2
    return 1


def crawl_site(
    base: str,
    fetcher: Fetcher,
    *,
    max_pages: int = 250,
    max_depth: int = 4,
) -> tuple[list[dict], list[str]]:
    """Crawl fallback for sites with no usable sitemap.

    Breadth-first, but ordered by ``crawl_priority`` within each depth so a bounded run
    spends its budget on catalogue branches. Bounded by ``max_pages`` and ``max_depth``
    so a bad site cannot run away with the run.
    """
    base_netloc = urlparse(base).netloc
    queue: list[tuple[int, int, str]] = [(0, 0, base + "/")]   # (depth, priority, url)
    seen: set[str] = {base + "/"}
    found: list[dict] = []
    errors: list[str] = []

    while queue and len(found) < max_pages:
        queue.sort(key=lambda item: (item[0], item[1]))
        depth, _, url = queue.pop(0)
        try:
            rec, html = fetcher.get(url)
        except Exception as exc:
            errors.append(f"crawl {url}: {type(exc).__name__}: {exc}")
            continue
        if rec.status != 200 or "html" not in rec.content_type.lower():
            continue

        found.append({"url": rec.final_url, "source": "crawl", "lastmod": None})
        if depth >= max_depth:
            continue
        for link in extract_links(html, rec.final_url, base_netloc):
            if link not in seen:
                seen.add(link)
                queue.append((depth + 1, crawl_priority(link), link))

    if queue:
        errors.append(
            f"crawl stopped at the {max_pages}-page budget with {len(queue)} URLs "
            f"still queued — coverage is partial"
        )
    return found, errors


# Sitemap *file names* say what they hold. Read product sitemaps first and e-commerce,
# media and community sitemaps last, so a URL budget is spent on the catalogue.
# agilent.com lists a 49,989-URL e-shop sitemap (pim_commerce01.xml) before
# products0.xml; read in declared order, the budget ran out before the products.
_SITEMAP_FIRST = re.compile(r"product|produkt|instrument|equipment|catalog|page", re.I)
_SITEMAP_LAST = re.compile(
    r"commerce|store|shop|sku|pim|video|multimedia|image|media|news|press|blog|post|"
    r"event|webinar|career|job|promotion|community|forum|support|training|author|tag|"
    r"categor(?:y|ies)_?tag|attachment",
    re.I,
)


def sitemap_priority(url: str, base_netloc: str) -> int:
    """Lower is read first. Sitemaps on other hosts (a community forum) go last."""
    parsed = urlparse(url)
    name = parsed.path.rsplit("/", 1)[-1] + "?" + parsed.query
    score = 1
    if _SITEMAP_FIRST.search(name) and not _SITEMAP_LAST.search(name):
        score = 0
    elif _SITEMAP_LAST.search(name):
        score = 2
    if base_netloc and parsed.netloc and parsed.netloc != base_netloc:
        score += 3
    return score


def base_host_note(domain: str, status: int, used: str) -> str:
    return f"{domain} answered {status} on the bare host; using {used} instead"


def detect_platform(html: str) -> str | None:
    for name, hints in PLATFORM_HINTS.items():
        if any(h in html for h in hints):
            return name
    return None


def discover_site(
    domain: str,
    fetcher: Fetcher,
    *,
    max_sitemaps: int = 50,
    crawl_fallback: bool = True,
    max_crawl_pages: int = 250,
    max_urls: int = 50_000,
) -> SiteProfile:
    """Stage 0 entry point. Builds a SiteProfile with every URL the site advertises."""
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    profile = SiteProfile(domain=urlparse(base).netloc, base_url=base)

    # Canonicalise the host before anything else. Plenty of vendors serve (or even
    # resolve) only on www: qinstruments.com has no DNS record at all, so fetching
    # robots.txt on the bare host fails while the site itself is perfectly reachable.
    # The requested domain stays the run's identity; only the fetch base moves.
    try:
        rec, home = fetcher.get(base + "/")

        # Not every vendor redirects the bare host to www. retsch.com answers 404 on
        # retsch.com and serves the site only on www.retsch.com, so following redirects
        # is not enough — the other host variant has to be tried explicitly.
        if rec.status >= 400:
            netloc = urlparse(base).netloc
            alternate = (
                f"https://{netloc[4:]}" if netloc.startswith("www.")
                else f"https://www.{netloc}"
            )
            try:
                alt_rec, alt_home = fetcher.get(alternate + "/")
                if alt_rec.status < 400:
                    base, rec, home = alternate, alt_rec, alt_home
                    profile.errors.append(
                        f"{base_host_note(profile.domain, rec.status, alternate)}"
                    )
            except Exception:
                pass

        final = urlparse(rec.final_url)
        if final.netloc and final.netloc != urlparse(base).netloc:
            base = f"{final.scheme}://{final.netloc}"
        profile.base_url = base
        profile.platform = detect_platform(home)
        # A 403 on the homepage after the browser-UA retry means bot protection, not a
        # missing sitemap. Crawling will fail the same way, so say so plainly rather
        # than returning an empty result that looks like an empty site.
        if rec.status in (401, 403, 406, 429):
            profile.blocked = True
            profile.errors.append(
                f"homepage returned {rec.status} even with a browser User-Agent — "
                f"the site blocks automated access; scraping it is not possible "
                f"without measures this tool does not implement"
            )
    except Exception as exc:
        profile.errors.append(f"homepage: {type(exc).__name__}: {exc}")

    try:
        _, robots_text = fetcher.get(f"{base}/robots.txt")
        profile.robots_sitemaps = parse_robots_sitemaps(robots_text)
    except Exception as exc:
        profile.robots_sitemaps = []
        profile.errors.append(f"robots: {type(exc).__name__}: {exc}")

    # Declared sitemaps first, then probe defaults — the reference site needs the probe.
    queue = list(profile.robots_sitemaps)
    if not queue:
        for path in SITEMAP_PROBE_PATHS:
            candidate = urljoin(base + "/", path.lstrip("/"))
            try:
                rec, text = fetcher.get(candidate)
            except Exception:
                continue
            if rec.status == 200 and looks_like_sitemap(text):
                queue.append(candidate)
                break

    seen_sitemaps: set[str] = set()
    base_netloc = urlparse(base).netloc
    while queue and len(seen_sitemaps) < max_sitemaps:
        queue.sort(key=lambda u: sitemap_priority(u, base_netloc))   # stable
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        try:
            _, xml = fetcher.get(sm_url)
        except Exception as exc:
            profile.errors.append(f"sitemap {sm_url}: {type(exc).__name__}: {exc}")
            continue
        if not looks_like_sitemap(xml):
            profile.errors.append(
                f"sitemap {sm_url}: status 200 but body is not a sitemap "
                f"({xml.strip()[:80]!r})"
            )
            continue
        profile.sitemaps_found.append(sm_url)
        is_index, entries = parse_sitemap(xml)
        if is_index:
            queue.extend(e["loc"] for e in entries)
        else:
            for e in entries:
                profile.urls.append(
                    {"url": e["loc"], "source": sm_url, "lastmod": e["lastmod"]}
                )
        # A URL budget, because some catalogues are effectively unbounded: thermofisher.com
        # declares several sitemap indexes including an antibody catalogue, and walking
        # them all never finishes. Stopping loudly beats grinding silently.
        if len(profile.urls) >= max_urls:
            profile.errors.append(
                f"stopped at the {max_urls:,}-URL budget with {len(queue)} sitemaps "
                f"unread — narrow the scope with a recipe, or raise max_urls"
            )
            break

    # No usable sitemap anywhere: fall back to a bounded breadth-first crawl.
    if not profile.urls and crawl_fallback and not profile.blocked:
        crawled, crawl_errors = crawl_site(
            profile.base_url, fetcher, max_pages=max_crawl_pages
        )
        profile.urls.extend(crawled)
        profile.errors.extend(crawl_errors[:10])
        profile.crawled = True

    return profile
