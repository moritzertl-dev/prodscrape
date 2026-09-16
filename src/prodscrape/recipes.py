"""Site recipes — load, save, and infer (PIPELINE.md §2).

A recipe freezes everything the agent discovered about a vendor so later runs need no
model calls. For a vendor with no recipe yet, ``infer_rules`` makes a deterministic first
guess from URL structure alone. The guess is always reported so a human (or the agent) can
correct it and save a real recipe.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .inventory import depth_histogram, path_segments
from .paths import recipe_search_path, user_recipe_dir

# Path segments that commonly root a product catalogue, across vendors and languages.
PRODUCT_PATH_TOKENS = (
    "products", "product", "produkte", "produkt",
    "shop", "catalog", "catalogue", "katalog",
    "instruments", "instrumente", "equipment", "geraete", "geräte",
    "portfolio", "range",
)

# Branches that are never products. Cheap, and they carry most of a site's URL volume.
DEFAULT_EXCLUDES = (
    "knowledge", "company", "news", "press", "blog", "stories", "events",
    "career", "careers", "jobs", "about", "support", "service", "service-support",
    "downloads", "search", "legal", "imprint", "privacy", "contact",
    "industries-solutions", "applications", "literature", "webinars",
)

LOCALE_SEGMENTS = {
    # language codes
    "en", "de", "fr", "es", "it", "nl", "pt", "ja", "zh", "ko", "pl", "cs", "cz",
    "ru", "bg", "hu", "ro", "sk", "sl", "hr", "tr", "sv", "da", "fi", "no", "el",
    "uk", "ua", "th", "vi", "id", "ar", "he",
    # region and language-region forms
    "us", "eu", "int", "global", "row",
    "en-us", "en-gb", "de-de", "de-at", "de-ch", "fr-fr", "es-es", "it-it",
    "int-en", "int-es", "us-en", "uk-en", "pl-pl", "fr-ch",
}


@dataclass
class Recipe:
    domain: str
    base_url: str = ""
    platform: str | None = None
    locale: str | None = None
    sitemap_urls: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    family_depth: int | None = None
    slug_suffix: str | None = None
    spec_headings: list[str] = field(default_factory=list)
    order_headings: list[str] = field(default_factory=list)
    threshold: float = 0.6
    spec_table_orientation: str | None = None
    inferred: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def recipe_path(domain: str) -> Path:
    """Where a recipe for this domain would be *written* (the user directory)."""
    return user_recipe_dir() / f"{domain}.yaml"


def find_recipe(domain: str) -> Path | None:
    """Locate a recipe, preferring a user override over the bundled default."""
    for directory in recipe_search_path():
        candidate = directory / f"{domain}.yaml"
        if candidate.exists():
            return candidate
    return None


def load_recipe(domain: str) -> Recipe | None:
    """Load a saved recipe, tolerating the richer nested YAML written by hand."""
    path = find_recipe(domain)
    if path is None:
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    urls = raw.get("product_urls", {})
    cls = raw.get("classification", {})
    ext = raw.get("extraction", {})
    disc = raw.get("discovery", {})

    return Recipe(
        domain=raw.get("domain", domain),
        base_url=raw.get("base_url", ""),
        platform=raw.get("platform"),
        locale=raw.get("locale"),
        sitemap_urls=disc.get("sitemap_urls", []),
        include=urls.get("include", []),
        exclude=urls.get("exclude", []),
        family_depth=urls.get("family_depth"),
        slug_suffix=urls.get("slug_suffix"),
        spec_headings=cls.get("spec_headings", []),
        order_headings=cls.get("order_headings", []),
        threshold=cls.get("threshold", 0.6),
        spec_table_orientation=ext.get("spec_table_orientation"),
        inferred=False,
        notes=raw.get("open_questions", []) or [],
    )


def save_recipe(recipe: Recipe, path: Path | None = None) -> Path:
    """Write a recipe in the same nested shape ``load_recipe`` reads."""
    path = path or recipe_path(recipe.domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": 1,
        "domain": recipe.domain,
        "base_url": recipe.base_url,
        "platform": recipe.platform,
        "locale": recipe.locale,
        "discovery": {"sitemap_urls": recipe.sitemap_urls},
        "product_urls": {
            "include": recipe.include,
            "exclude": recipe.exclude,
            "family_depth": recipe.family_depth,
            "slug_suffix": recipe.slug_suffix,
        },
        "classification": {
            "spec_headings": recipe.spec_headings,
            "order_headings": recipe.order_headings,
            "threshold": recipe.threshold,
        },
        "extraction": {"spec_table_orientation": recipe.spec_table_orientation},
        "open_questions": recipe.notes,
    }
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def _strip_locale(segments: list[str]) -> list[str]:
    if segments and segments[0].lower() in LOCALE_SEGMENTS:
        return segments[1:]
    return segments


def infer_rules(urls: list[str], domain: str) -> Recipe:
    """Deterministically guess product-URL rules from site structure. No model calls.

    Strategy: find the catalogue root segment, then pick the path depth that holds the
    most URLs below it — leaf product pages nearly always outnumber the taxonomy pages
    above them.
    """
    notes: list[str] = []

    # Choose the catalogue prefix as a whole path, not a locale and a root picked
    # independently. Retsch translates its path segments per locale — /bg/products/ but
    # /de/produkte/ — so combining the commonest locale with the commonest root produced
    # /de/products/, a path that exists nowhere on the site.
    catalogue_prefixes: Counter[str] = Counter()
    fallback_prefixes: Counter[str] = Counter()

    for url in urls:
        segs = [s.lower() for s in path_segments(url)]
        if not segs:
            continue
        body = _strip_locale(segs)
        if not body:
            continue
        lead = segs[: len(segs) - len(body)]          # the locale prefix, if any
        if body[0] in PRODUCT_PATH_TOKENS:
            catalogue_prefixes["/" + "/".join(lead + [body[0]])] += 1
        elif body[0] not in DEFAULT_EXCLUDES:
            fallback_prefixes["/" + "/".join(lead + [body[0]])] += 1

    if catalogue_prefixes:
        root_path, hits = catalogue_prefixes.most_common(1)[0]
        notes.append(
            f"catalogue prefix {root_path!r} matched a known product path token "
            f"({hits} URLs)"
        )
    elif fallback_prefixes:
        root_path, hits = fallback_prefixes.most_common(1)[0]
        notes.append(
            f"no standard product path token found; guessed largest non-editorial "
            f"branch {root_path!r} ({hits} URLs) — verify this"
        )
    else:
        return Recipe(domain=domain, inferred=True,
                      notes=["could not identify a product branch from URL structure"])

    first = root_path.strip("/").split("/")[0]
    locale = first if first in LOCALE_SEGMENTS else None
    prefix_parts = [locale] if locale else []
    hist = depth_histogram(urls, prefix=root_path + "/")
    deep = {d: len(v) for d, v in hist.items() if d >= len(prefix_parts) + 2}
    if not deep:
        deep = {d: len(v) for d, v in hist.items()}

    family_depth = max(deep, key=lambda d: deep[d]) if deep else None
    if family_depth is not None:
        notes.append(
            f"family_depth={family_depth} holds {deep[family_depth]} URLs "
            f"(depth counts: {deep})"
        )

    return Recipe(
        domain=domain,
        locale=locale,
        include=[f"{root_path}/*"],
        exclude=[f"/{e}/*" for e in DEFAULT_EXCLUDES] + [f"/{locale}/{e}/*" for e in DEFAULT_EXCLUDES]
        if locale
        else [f"/{e}/*" for e in DEFAULT_EXCLUDES],
        family_depth=family_depth,
        inferred=True,
        notes=notes,
    )
