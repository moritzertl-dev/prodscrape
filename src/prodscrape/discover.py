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


def crawl_site(
    base: str,
    fetcher: Fetcher,
    *,
    max_pages: int = 250,
    max_depth: int = 4,
) -> tuple[list[dict], list[str]]:
    """Breadth-first crawl fallback for sites with no usable sitemap.

    Bounded by ``max_pages`` and ``max_depth`` so a bad site cannot run away with the
    run. Returns ``(urls, errors)``; URLs carry the depth they were found at.
    """
    base_netloc = urlparse(base).netloc
    queue: list[tuple[str, int]] = [(base + "/", 0)]
    seen: set[str] = {base + "/"}
    found: list[dict] = []
    errors: list[str] = []

    while queue and len(found) < max_pages:
        url, depth = queue.pop(0)
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
                queue.append((link, depth + 1))

    return found, errors


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
        final = urlparse(rec.final_url)
        if final.netloc and final.netloc != urlparse(base).netloc:
            base = f"{final.scheme}://{final.netloc}"
        profile.base_url = base
        profile.platform = detect_platform(home)
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
    while queue and len(seen_sitemaps) < max_sitemaps:
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

    # No usable sitemap anywhere: fall back to a bounded breadth-first crawl.
    if not profile.urls and crawl_fallback:
        crawled, crawl_errors = crawl_site(
            profile.base_url, fetcher, max_pages=max_crawl_pages
        )
        profile.urls.extend(crawled)
        profile.errors.extend(crawl_errors[:10])
        profile.crawled = True

    return profile
