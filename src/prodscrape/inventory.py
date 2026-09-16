"""Stage 1 — URL triage.

The context-economy move (PIPELINE.md §1): never hand the agent a raw URL list. Return a
path-prefix tree with counts and a few samples per branch, so a 40k-URL site is summarised
in ~40 lines and the agent can pick product branches for a handful of tokens.
"""

from __future__ import annotations

import fnmatch
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse


def path_segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.split("/") if s]


def url_depth(url: str) -> int:
    return len(path_segments(url))


@dataclass
class Branch:
    prefix: str
    count: int
    depth: int
    samples: list[str]


def prefix_tree(
    urls: list[str], *, max_depth: int = 3, samples_per_branch: int = 3
) -> list[Branch]:
    """Aggregate URLs into a prefix tree summary, sorted by path."""
    counts: Counter[str] = Counter()
    samples: defaultdict[str, list[str]] = defaultdict(list)
    for url in urls:
        segs = path_segments(url)
        for d in range(1, min(len(segs), max_depth) + 1):
            prefix = "/" + "/".join(segs[:d])
            counts[prefix] += 1
            if len(samples[prefix]) < samples_per_branch:
                samples[prefix].append(url)
    return [
        Branch(prefix=p, count=counts[p], depth=p.count("/"), samples=samples[p])
        for p in sorted(counts)
    ]


def render_tree(branches: list[Branch], *, min_count: int = 1) -> str:
    """Human/agent-readable rendering. This is what the MCP tool returns."""
    lines = []
    for b in branches:
        if b.count < min_count:
            continue
        lines.append(f"{'  ' * (b.depth - 1)}{b.prefix}  [{b.count}]")
    return "\n".join(lines)


def depth_histogram(urls: list[str], prefix: str = "") -> dict[int, list[str]]:
    """Group URLs by path depth under an optional prefix.

    Depth is the cheapest product/taxonomy discriminator there is: on the reference site
    product families sit at depth 5 while depths 1-4 are category landing pages.
    """
    out: defaultdict[int, list[str]] = defaultdict(list)
    for url in urls:
        path = urlparse(url).path
        if prefix and not path.startswith(prefix):
            continue
        out[url_depth(url)].append(url)
    return dict(sorted(out.items()))


def _normalised_path(url: str) -> str:
    path = urlparse(url).path
    return path if path.endswith("/") else path + "/"


def collapse_query_variants(urls: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Collapse URLs that differ only by query string onto one canonical page.

    Catalogues commonly expose the same page once per SKU — ``/pipette?part-number=1``,
    ``?part-number=2`` and so on. Treated as separate candidates they are fetched dozens
    of times, and each copy becomes its own device row.

    Returns ``(canonical_urls, collapsed)``, where ``collapsed`` maps each canonical URL
    to the variants folded into it — nothing is discarded silently, and the part numbers
    remain available.
    """
    groups: dict[str, list[str]] = {}
    for url in urls:
        parsed = urlparse(url)
        key = parsed._replace(query="", fragment="").geturl()
        groups.setdefault(key, []).append(url)

    canonical: list[str] = []
    collapsed: dict[str, list[str]] = {}
    for key, members in groups.items():
        # Prefer the bare URL if the site publishes one; otherwise keep the first variant
        # so the page is still reachable.
        bare = next((m for m in members if not urlparse(m).query), None)
        chosen = bare or sorted(members)[0]
        canonical.append(chosen)
        others = [m for m in members if m != chosen]
        if others:
            collapsed[chosen] = sorted(others)
    return canonical, collapsed


def leaf_urls(urls: list[str]) -> set[str]:
    """URLs that no other URL in the set extends.

    A category page has children in the sitemap; a product page does not. This is the
    structural difference between the two, and unlike a fixed depth it holds however deep
    a vendor happens to nest a given branch.

    Hamilton was the case that forced this: its catalogue puts products at several depths,
    so a single ``family_depth`` silently skipped every product that did not happen to sit
    at the chosen level.
    """
    paths = sorted({_normalised_path(u) for u in urls})
    leaves: set[str] = set()
    for i, path in enumerate(paths):
        nxt = paths[i + 1] if i + 1 < len(paths) else None
        if nxt is None or not nxt.startswith(path):
            leaves.add(path)
    return {u for u in urls if _normalised_path(u) in leaves}


def leaf_depth_distribution(urls: list[str]) -> dict[int, int]:
    """How leaves are spread across depths — the evidence for or against a depth rule."""
    leaves = leaf_urls(urls)
    counts: Counter[int] = Counter(url_depth(u) for u in leaves)
    return dict(sorted(counts.items()))


def select_candidates(
    urls: list[str],
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    depth: int | None = None,
    depths: list[int] | None = None,
    leaf_only: bool = False,
    slug_suffix: str | None = None,
) -> list[str]:
    """Apply recipe rules to shortlist candidate product URLs.

    ``leaf_only`` selects pages with no children, which is depth-agnostic and the
    preferred rule. ``depths`` accepts several levels; ``depth`` remains for recipes
    written against the older single-level field.

    All filters are deterministic and free — this is tier 1 of the token economy, and on a
    well-structured site it does nearly all the work before any model call.
    """
    include = include or ["**"]
    exclude = exclude or []

    allowed_depths: set[int] | None = None
    if depths:
        allowed_depths = set(depths)
    elif depth is not None:
        allowed_depths = {depth}

    scoped = [
        u for u in urls
        if any(fnmatch.fnmatch(urlparse(u).path, p) for p in include)
        and not any(fnmatch.fnmatch(urlparse(u).path, p) for p in exclude)
    ]
    # Leaf-ness is judged against the whole site, not the filtered subset: a product page
    # excluded from `include` can still prove that its parent is a category page.
    leaves = leaf_urls(urls) if leaf_only else None

    out = []
    for url in scoped:
        if allowed_depths is not None and url_depth(url) not in allowed_depths:
            continue
        if leaves is not None and url not in leaves:
            continue
        if slug_suffix is not None:
            segs = path_segments(url)
            if not segs or not segs[-1].endswith(slug_suffix):
                continue
        out.append(url)
    return out
