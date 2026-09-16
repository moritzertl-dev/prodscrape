"""Stage 0 regression tests."""

from __future__ import annotations

from prodscrape.discover import (
    detect_platform,
    parse_robots_sitemaps,
    parse_sitemap,
)


def test_robots_without_sitemap_directive_returns_empty(cache):
    """The reference site declares no Sitemap: line - the probe path must stay reachable."""
    hit = cache.get("https://www.analytik-jena.com/robots.txt")
    assert hit is not None
    assert parse_robots_sitemaps(hit[1]) == []


def test_robots_with_sitemap_directive_is_parsed():
    robots = "User-agent: *\nDisallow: /admin/\nSitemap: https://example.com/sitemap.xml\n"
    assert parse_robots_sitemaps(robots) == ["https://example.com/sitemap.xml"]


def test_sitemap_index_is_recognised_and_unescaped(cache):
    hit = cache.get("https://www.analytik-jena.com/sitemap.xml")
    assert hit is not None
    is_index, entries = parse_sitemap(hit[1])
    assert is_index is True
    assert len(entries) == 1
    # The loc carries an &amp;-escaped cHash query; unescaping it is what makes the
    # follow-up fetch return 200 instead of 404.
    assert "&amp;" not in entries[0]["loc"]
    assert "sitemap=pages" in entries[0]["loc"]


def test_urlset_is_not_an_index(cache):
    hit = cache.get(
        "https://www.analytik-jena.com/sitemap.xml"
        "?sitemap=pages&cHash=a3f16e5fd5f74c74540166e3577d0968"
    )
    assert hit is not None
    is_index, entries = parse_sitemap(hit[1])
    assert is_index is False
    assert len(entries) > 800


def test_platform_detection(plasmaquant_html):
    assert detect_platform(plasmaquant_html) == "typo3"


def test_discovery_found_expected_url_count(site_urls):
    assert len(site_urls) > 800
    assert sum(1 for u in site_urls if "/products/" in u) > 80


def test_broken_sitemap_is_rejected():
    """qinstruments.com declares /sitemap.xml in robots.txt; it returns HTTP 200 with
    the body "Invalid error handler configuration: t3://page?uid=234". A 200 status is
    not evidence of a sitemap."""
    from prodscrape.discover import looks_like_sitemap

    assert looks_like_sitemap("Invalid error handler configuration: t3://page?uid=234") is False
    assert looks_like_sitemap('<?xml version="1.0"?><urlset><url><loc>x</loc></url></urlset>')


def test_extract_links_filters_to_crawlable_internal_pages():
    from prodscrape.discover import extract_links

    html = """
    <a href="/products/shaker">ok</a>
    <a href="/products/shaker#specs">dup via fragment</a>
    <a href="https://other.test/x">external</a>
    <a href="/files/datasheet.pdf">asset</a>
    <a href="mailto:a@b.test">mail</a>
    <a href="#top">anchor</a>
    <a href="https://www.x.test/about">absolute internal</a>
    """
    links = extract_links(html, "https://www.x.test/home", "www.x.test")
    assert links == ["https://www.x.test/products/shaker", "https://www.x.test/about"]
