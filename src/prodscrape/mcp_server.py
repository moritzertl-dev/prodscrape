"""MCP server — the tool surface an agent drives.

Design rules, all of them load-bearing:

1. **No tool ever returns a page.** Tools return summaries, compact digests and file
   paths. The largest thing that crosses this boundary is a ~300-token page digest.
2. **The agent is the judgment layer.** There is no LLM client in this codebase. Stage-2
   tier B and the Stage-3.6 review are not "call a model" — they are `pending_*` tools
   that hand Claude the ambiguous minority, and `record_*` tools that write its verdicts
   to disk.
3. **Judgments persist.** Every verdict is stored and replayed, so a re-run never re-asks
   a question and costs nothing.

Run with:  ``uv run python -m prodscrape.mcp_server``
"""

from __future__ import annotations

import json
from pathlib import Path

# mcp 2.x renamed FastMCP to MCPServer; the decorator API is unchanged.
from mcp.server.mcpserver import MCPServer

from .costs import (
    DEFAULT_MODEL, TOKENS_PER_VERDICT, compare_naive, estimate_scrape, price,
)
from .digest import digest_size_estimate, page_digest
from .discover import discover_site
from .fetch import Cache, Fetcher
from .inventory import prefix_tree, render_tree
from .paths import cache_dir, describe, runs_dir
from .pipeline import run_extract, run_scan
from .recipes import Recipe, infer_rules, load_recipe, save_recipe
from .verdicts import VerdictStore

mcp = MCPServer("prodscrape")


def _run_dir(domain: str) -> Path:
    return runs_dir() / domain.replace("https://", "").replace("http://", "").strip("/")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------- stage 0-1

@mcp.tool()
def site_overview(domain: str, delay: float = 1.0) -> dict:
    """Profile a vendor site: platform, sitemap health, URL prefix tree, suggested rules.

    Cheap — a handful of HTTP requests, no product pages fetched. Always the first call
    for an unfamiliar vendor. Returns the prefix tree as text, never the raw URL list.
    """
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    cache = Cache(cache_dir() / domain)
    with Fetcher(cache, delay=delay) as fetcher:
        profile = discover_site(domain, fetcher)
    urls = [u["url"] for u in profile.urls]

    recipe = load_recipe(profile.domain) or load_recipe(domain)
    suggested = None if recipe else infer_rules(urls, profile.domain)
    return {
        "domain": profile.domain,
        "fetch_base": profile.base_url,
        "platform": profile.platform,
        "sitemap_declared_in_robots": profile.sitemap_declared_in_robots,
        "robots_readable": profile.robots_ok,
        "discovered_via_crawl": profile.crawled,
        "total_urls": len(urls),
        "errors": profile.errors[:5],
        "prefix_tree": render_tree(prefix_tree(urls, max_depth=3)),
        "saved_recipe": recipe.to_dict() if recipe else None,
        "suggested_rules": suggested.to_dict() if suggested else None,
    }


@mcp.tool()
def get_recipe(domain: str) -> dict:
    """Read the saved per-vendor recipe, or report that none exists."""
    recipe = load_recipe(domain)
    return recipe.to_dict() if recipe else {"domain": domain, "exists": False}


@mcp.tool()
def put_recipe(
    domain: str,
    include: list[str],
    family_depth: int | None = None,
    family_depths: list[int] | None = None,
    leaf_only: bool = True,
    exclude: list[str] | None = None,
    locale: str | None = None,
    spec_headings: list[str] | None = None,
    order_headings: list[str] | None = None,
    slug_suffix: str | None = None,
    spec_table_orientation: str | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Save the rules for a vendor so later runs need no exploration.

    This is what makes re-runs free: with a recipe on disk the whole scan is
    deterministic. `spec_headings`/`order_headings` extend the built-in defaults, so a
    partial recipe can never classify worse than none.

    Candidate selection defaults to `leaf_only=True` — pages with no children, at any
    depth. Prefer it: catalogues nest products at several levels, and a single
    `family_depth` silently skips every product that sits elsewhere. `family_depth` is
    kept only as a scoring hint; set `family_depths` if a vendor really needs an explicit
    whitelist of levels.
    """
    recipe = Recipe(
        domain=domain,
        include=include,
        exclude=exclude or [],
        family_depth=family_depth,
        family_depths=family_depths or [],
        leaf_only=leaf_only,
        locale=locale,
        spec_headings=spec_headings or [],
        order_headings=order_headings or [],
        slug_suffix=slug_suffix,
        spec_table_orientation=spec_table_orientation,
        notes=notes or [],
    )
    path = save_recipe(recipe)
    return {"saved": str(path), "recipe": recipe.to_dict()}


# --------------------------------------------------------------------------- stage 2

@mcp.tool()
def scan_site(domain: str, limit: int = 0, delay: float = 1.0) -> dict:
    """Shortlist candidate product pages and classify them deterministically.

    `limit=0` means no cap. Fetches are cached, so re-running is fast and free. Returns
    counts and artifact paths — the per-page results stay on disk.
    """
    return run_scan(domain, limit=None if limit == 0 else limit, delay=delay)


@mcp.tool()
def pending_classifications(domain: str, limit: int = 20) -> dict:
    """Pages the structural signals could not decide — your tier-B queue.

    Returns a compact digest per page (a few hundred tokens each), never the page. Judge
    each one and send the verdicts to `record_classifications`.

    Labels: instrument, accessory, consumable, software, service, category_page, other.
    Only `instrument` proceeds to extraction.
    """
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    out = _run_dir(domain)
    cache = Cache(cache_dir() / domain)
    store = VerdictStore.load(out)

    rows = [
        r for r in _read_jsonl(out / "classified.jsonl")
        if r["label"] == "unknown" and store.classification_for(r["url"]) is None
    ]
    digests, tokens = [], 0
    for row in rows[:limit]:
        hit = cache.get(row["url"])
        if hit is None:
            continue
        digest = page_digest(row["url"], hit[1])
        digest["signal_confidence"] = row["confidence"]
        digest["signal_reason"] = row["reason"]
        tokens += digest_size_estimate(digest)
        digests.append(digest)

    # Tally what actually crossed the boundary, so the end-of-run figure is measured
    # rather than re-estimated.
    if digests:
        store.add_usage(tokens, output_tokens=len(digests) * TOKENS_PER_VERDICT)
        store.save()

    est = price(DEFAULT_MODEL, tokens, len(digests) * TOKENS_PER_VERDICT)
    return {
        "domain": domain,
        "pending_total": len(rows),
        "returned": len(digests),
        "approx_tokens": tokens,
        "cost_of_this_batch": est.as_dict(),
        "digests": digests,
    }


@mcp.tool()
def record_classifications(domain: str, verdicts: list[dict]) -> dict:
    """Persist tier-B labels. Each verdict: {url, label, reason}.

    Stored to `verdicts.json` and replayed on every later scan, so the same page is never
    judged twice.
    """
    out = _run_dir(domain)
    store = VerdictStore.load(out)
    applied, errors = 0, []
    for v in verdicts:
        try:
            store.set_classification(v["url"], v["label"], v.get("reason", ""))
            applied += 1
        except (KeyError, ValueError) as exc:
            errors.append(f"{v.get('url', '?')}: {exc}")
    store.save()
    return {
        "applied": applied,
        "errors": errors,
        "totals": store.counts,
        "next": "re-run scan_site to fold these in, then extract_devices",
    }


# --------------------------------------------------------------------------- stage 3-4

@mcp.tool()
def extract_devices(domain: str, manufacturer: str | None = None) -> dict:
    """Build the device table from a completed scan. Fully offline.

    One row per device; variants that differ only in non-functional attributes (mains
    voltage, fuse rating, article number, bundled software) are merged. Writes
    `devices.csv`, `specs_eav.csv` and `review_queue.csv`.
    """
    return run_extract(domain, manufacturer=manufacturer)


@mcp.tool()
def pending_reviews(domain: str, limit: int = 30) -> dict:
    """Records with no specification table — usually series or overview pages.

    Having no spec table is a strong indicator of a non-device, but not proof, so these
    are held rather than dropped. Judge each and send verdicts to `record_reviews`.
    Verdict is "device" or "not-a-device".
    """
    out = _run_dir(domain)
    store = VerdictStore.load(out)
    rows = [
        {
            "product_id": r["product_id"],
            "name": r["name"],
            "category": r["category"],
            "url": r["url"],
            "description": r["description"][:300],
            "interfaces": r["interfaces"],
            "datasheet_count": len(r["datasheet_urls"]),
            "reason_held": r["warnings"],
        }
        for r in _read_jsonl(out / "extracted.jsonl")
        if not r["specs"] and store.review_for(r["product_id"]) is None
    ]
    return {"domain": domain, "pending_total": len(rows), "rows": rows[:limit]}


@mcp.tool()
def record_reviews(domain: str, verdicts: list[dict]) -> dict:
    """Persist review verdicts. Each: {product_id, verdict, reason}.

    A verdict of "device" promotes the record into `devices.csv` on the next
    `extract_devices` call even though it has no specifications.
    """
    out = _run_dir(domain)
    store = VerdictStore.load(out)
    applied, errors = 0, []
    for v in verdicts:
        try:
            store.set_review(v["product_id"], v["verdict"], v.get("reason", ""))
            applied += 1
        except (KeyError, ValueError) as exc:
            errors.append(f"{v.get('product_id', '?')}: {exc}")
    store.save()
    return {
        "applied": applied,
        "errors": errors,
        "totals": store.counts,
        "next": "re-run extract_devices to apply these",
    }


@mcp.tool()
def device_table(domain: str, limit: int = 25, category: str | None = None) -> dict:
    """Preview the finished table: core columns per device, specs summarised by count.

    Use `export_table` for the full file path; this is for looking, not bulk transfer.
    """
    rows = _read_jsonl(_run_dir(domain) / "extracted.jsonl")
    rows = [r for r in rows if r["specs"]]
    if category:
        rows = [r for r in rows if r["category"] == category]
    preview = [
        {
            "name": r["name"],
            "category": r["category"],
            "interfaces": r["interfaces"],
            "spec_count": len(r["specs"]),
            "url": r["url"],
        }
        for r in rows[:limit]
    ]
    return {
        "total_devices": len(rows),
        "returned": len(preview),
        "categories": sorted({r["category"] for r in rows}),
        "rows": preview,
    }


@mcp.tool()
def device_specs(domain: str, name: str) -> dict:
    """Every specification of one device, with raw source text preserved."""
    for row in _read_jsonl(_run_dir(domain) / "extracted.jsonl"):
        if row["name"].lower() == name.lower():
            return {
                "name": row["name"],
                "url": row["url"],
                "interfaces": row["interfaces"],
                "merged_from": row["source_variants"],
                "merge_reason": row["merge_reason"],
                "specs": {k: v.get("raw") for k, v in row["specs"].items()},
            }
    return {"error": f"no device named {name!r}", "hint": "call device_table first"}


@mcp.tool()
def export_table(domain: str, category: str | None = None) -> dict:
    """Paths to the exported files, plus a category-pivoted view when asked.

    Passing a `category` promotes that category's common attributes into real columns —
    the "category profile" as a view over the same data, needing no re-scrape.
    """
    from .export import pivot_category, write_csv
    from .extract import DeviceRecord
    from .normalize import SpecValue

    out = _run_dir(domain)
    result = {
        "devices_csv": str(out / "devices.csv"),
        "specs_eav_csv": str(out / "specs_eav.csv"),
        "review_queue_csv": str(out / "review_queue.csv"),
        "extracted_jsonl": str(out / "extracted.jsonl"),
    }
    if category:
        records = [
            DeviceRecord(
                product_id=r["product_id"], manufacturer=r["manufacturer"],
                name=r["name"], url=r["url"], category=r["category"],
                specs={k: SpecValue(**v) for k, v in r["specs"].items()},
            )
            for r in _read_jsonl(out / "extracted.jsonl") if r["specs"]
        ]
        rows, columns = pivot_category(records, category)
        if rows:
            path = out / f"category_{category}.csv"
            write_csv(rows, path, columns)
            result["category_csv"] = str(path)
            result["category_columns"] = columns
            result["category_rows"] = len(rows)
        else:
            result["category_csv"] = None
            result["note"] = f"no devices in category {category!r}"
    return result


@mcp.tool()
def estimate_cost(domain: str, model: str = DEFAULT_MODEL) -> dict:
    """What this vendor is expected to cost in model calls, before you start.

    Call this at the beginning of a run and report the figure. It prices only the
    escalated minority — the deterministic tiers are free — and excludes the agent's own
    conversation context, which is billed separately and is not visible from here.
    """
    out = _run_dir(domain)
    candidates = len(_read_jsonl(out / "candidates.jsonl"))
    extracted = _read_jsonl(out / "extracted.jsonl")
    review_rows = sum(1 for r in extracted if not r["specs"])

    if not candidates:
        return {
            "domain": domain,
            "note": "no scan yet — run site_overview then scan_site first",
        }

    est = estimate_scrape(candidates, model=model, review_rows=review_rows)
    naive = compare_naive(candidates, model=model)
    return {
        "domain": domain,
        "candidates": candidates,
        "estimate": est.as_dict(),
        "if_whole_pages_were_sent": naive.as_dict(),
        "saving_factor": round(naive.total_cost / max(est.total_cost, 1e-9)),
        "summary": est.summary(),
    }


@mcp.tool()
def cost_report(domain: str, model: str = DEFAULT_MODEL) -> dict:
    """What this vendor actually cost — tokens measured as they were handed over.

    Call this at the end of a run and report the figure alongside the estimate. This is a
    floor, not the full bill: it counts every digest and queue the pipeline passed to you,
    but not your own conversation context.
    """
    store = VerdictStore.load(_run_dir(domain))
    usage = store.usage
    actual = price(model, usage["input_tokens"], usage["output_tokens"])
    return {
        "domain": domain,
        "model_calls_made": usage["calls"],
        "verdicts_recorded": store.counts,
        "actual": actual.as_dict(),
        "summary": (
            f"{actual.total_tokens:,} tokens handed to the model across "
            f"{usage['calls']} batches = ${actual.total_cost:.4f} at {model} rates. "
            "Excludes your own conversation context, which is billed separately."
        ),
    }


@mcp.tool()
def storage_paths() -> dict:
    """Where runs, caches and recipes are read from and written to.

    Set PRODSCRAPE_HOME to relocate them; inside a source checkout they stay in the
    checkout.
    """
    return describe()


@mcp.tool()
def run_status(domain: str) -> dict:
    """What has been done for this vendor and what is outstanding."""
    out = _run_dir(domain)
    store = VerdictStore.load(out)
    classified = _read_jsonl(out / "classified.jsonl")
    extracted = _read_jsonl(out / "extracted.jsonl")
    return {
        "domain": domain,
        "recipe_saved": load_recipe(domain) is not None,
        "scanned": bool(classified),
        "classified_total": len(classified),
        "classified_unknown": sum(1 for r in classified if r["label"] == "unknown"),
        "extracted": bool(extracted),
        "devices": sum(1 for r in extracted if r["specs"]),
        "awaiting_review": sum(
            1 for r in extracted
            if not r["specs"] and store.review_for(r["product_id"]) is None
        ),
        "verdicts_recorded": store.counts,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
