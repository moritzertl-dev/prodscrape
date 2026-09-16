"""Command line entry point.

    prodscrape tree  <domain>            # stage 0-1 only: what does this site look like?
    prodscrape scan  <domain>            # stages 0-2: shortlist + classify
    prodscrape show  <domain>            # re-print the last run's summary
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .discover import discover_site
from .fetch import Cache, Fetcher
from .inventory import depth_histogram, prefix_tree, render_tree
from .paths import cache_dir, describe, runs_dir
from .pipeline import run_extract, run_scan
from .recipes import infer_rules, load_recipe


def _cmd_tree(args: argparse.Namespace) -> int:
    """Cheap reconnaissance: 3 HTTP requests, no page fetches, no model calls."""
    domain = args.domain.replace("https://", "").replace("http://", "").strip("/")
    cache = Cache(cache_dir() / domain)
    with Fetcher(cache, delay=args.delay) as fetcher:
        profile = discover_site(domain, fetcher)
    urls = [u["url"] for u in profile.urls]

    print(f"domain            : {profile.domain}")
    print(f"fetch base        : {profile.base_url}")
    print(f"platform          : {profile.platform}")
    print(
        f"sitemap in robots : {profile.sitemap_declared_in_robots}"
        + ("" if profile.robots_ok else "   (robots.txt UNREADABLE - value not trustworthy)")
    )
    print(f"sitemaps found    : {len(profile.sitemaps_found)}")
    print(f"total urls        : {len(urls)}")
    for err in profile.errors:
        print(f"  ! {err}")
    if not urls:
        if profile.blocked:
            print("\nThe site blocks automated access — 403 even with a browser "
                  "User-Agent.\nThis is not a missing sitemap; report the vendor as "
                  "unscrapeable by this tool.")
        else:
            print("\nNo URLs discovered: no usable sitemap, and the crawl found no "
                  "followable links.")
        return 1

    print("\n--- path prefix tree (this is what the agent sees) ---")
    print(render_tree(prefix_tree(urls, max_depth=args.depth)))

    recipe = load_recipe(profile.domain)
    if recipe:
        print(f"\n--- saved recipe: include={recipe.include} family_depth={recipe.family_depth}")
    else:
        guess = infer_rules(urls, profile.domain)
        print("\n--- inferred rules (no saved recipe) ---")
        print(f"include      : {guess.include}")
        print(f"family_depth : {guess.family_depth}")
        for note in guess.notes:
            print(f"  note: {note}")
        if guess.include:
            hist = depth_histogram(urls, prefix=guess.include[0].rstrip("*"))
            print(f"depth counts : { {d: len(v) for d, v in hist.items()} }")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    manifest = run_scan(
        args.domain,
        limit=None if args.limit == 0 else args.limit,
        delay=args.delay,
    )
    out = runs_dir() / manifest["domain"]
    print(json.dumps({k: v for k, v in manifest.items() if k != "recipe"}, indent=2))
    print(f"\nartifacts -> {out}/")
    for name in ("urls_raw.jsonl", "candidates.jsonl", "classified.jsonl",
                 "summary.md", "tree.txt", "manifest.json"):
        path = out / name
        if path.exists():
            print(f"  {name:22s} {path.stat().st_size:>9,} bytes")
    print(f"\nRead {out / 'summary.md'} for the human-reviewable table.")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    summary = run_extract(args.domain, manufacturer=args.manufacturer)
    print(json.dumps(summary, indent=2))
    out = runs_dir() / summary["domain"]
    print(f"\nartifacts -> {out}/")
    for name in ("extracted.jsonl", "devices.csv", "specs_eav.csv", "review_queue.csv"):
        path = out / name
        if path.exists():
            print(f"  {name:22s} {path.stat().st_size:>9,} bytes")
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    """Show where artifacts are read from and written to."""
    for key, value in describe().items():
        print(f"{key:18s} {value}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    path = runs_dir() / args.domain / "summary.md"
    if not path.exists():
        print(f"no run found at {path}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prodscrape", description=__doc__)
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between requests to the same host (default 1.0)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tree = sub.add_parser("tree", help="stages 0-1: site structure only, no page fetches")
    p_tree.add_argument("domain")
    p_tree.add_argument("--depth", type=int, default=3)
    p_tree.set_defaults(func=_cmd_tree)

    p_scan = sub.add_parser("scan", help="stages 0-2: shortlist and classify candidates")
    p_scan.add_argument("domain")
    p_scan.add_argument("--limit", type=int, default=40,
                        help="max pages to fetch; 0 means no cap (default 40)")
    p_scan.set_defaults(func=_cmd_scan)

    p_extract = sub.add_parser(
        "extract", help="stages 3-4: device rows + specs from a completed scan (offline)"
    )
    p_extract.add_argument("domain")
    p_extract.add_argument("--manufacturer", default=None)
    p_extract.set_defaults(func=_cmd_extract)

    p_paths = sub.add_parser("paths", help="show where artifacts and recipes live")
    p_paths.set_defaults(func=_cmd_paths)

    p_show = sub.add_parser("show", help="re-print a previous run's summary")
    p_show.add_argument("domain")
    p_show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
