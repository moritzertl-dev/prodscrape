"""Shared fixtures. Every test runs off the on-disk HTTP cache: offline, deterministic, free."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prodscrape.fetch import Cache

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture(scope="session")
def cache() -> Cache:
    return Cache(GOLDEN / "cache")


@pytest.fixture(scope="session")
def golden_labels() -> list[dict]:
    data = json.loads((GOLDEN / "labels_analytik_jena.json").read_text(encoding="utf-8"))
    return data["items"]


@pytest.fixture(scope="session")
def site_urls() -> list[str]:
    return json.loads((GOLDEN / "urls_analytik_jena.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def plasmaquant_html(cache: Cache) -> str:
    url = (
        "https://www.analytik-jena.com/products/chemical-analysis/"
        "elemental-analysis/icp-ms/plasmaquant-ms-series/"
    )
    hit = cache.get(url)
    assert hit is not None, "golden cache missing the PlasmaQuant page"
    return hit[1]
