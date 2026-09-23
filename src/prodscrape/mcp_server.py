"""MCP server — the tool surface an agent drives.

Design rules, all of them load-bearing:

1. **No tool ever returns a page.** Tools return summaries, compact digests and file
   paths. The largest thing that crosses this boundary is a ~300-token page digest.
2. **Judgment has two paths, one shape.** With a reasoning backend (Anthropic API key,
   or the `claude` CLI on the machine) `catalogue_vendor` asks the model itself, inside
   the run, and meters every call. Without one, the same questions reach the driving
   agent through `pending_*` tools and its answers come back through `record_*` tools.
   Verdicts land in the same store either way.
3. **Judgments persist.** Every verdict is stored and replayed, so a re-run never re-asks
   a question and costs nothing.
4. **Every response is metered.** Each tool result's size is appended to the vendor's
   ledger, so the cost report counts what the agent was actually handed instead of
   relying on the agent to remember — or estimate — it.

Run with:  ``uv run python -m prodscrape.mcp_server``
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from pathlib import Path

# mcp 2.x renamed FastMCP to MCPServer; the decorator API is unchanged.
from mcp.server.mcpserver import MCPServer

from .costs import calibrated_estimate, estimate_tokens
from .digest import digest_for_row
from .llm import Ledger
from .discover import discover_site
from .fetch import Cache, Fetcher
from .inventory import prefix_tree, render_tree
from .paths import cache_dir, describe, runs_dir
from .pipeline import run_extract, run_scan, table_records
from .recipes import Recipe, infer_rules, load_recipe, save_recipe
from .verdicts import VerdictStore

mcp = MCPServer("prodscrape")


def _clean(domain: str) -> str:
    return domain.replace("https://", "").replace("http://", "").strip("/")


def metered(fn):
    """Register a tool and record the size of everything it returns to the agent.

    The agent's own reasoning is invisible from here, but every byte a tool hands it is
    not — so it is counted at the source rather than reconstructed afterwards.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        try:
            bound = inspect.signature(fn).bind_partial(*args, **kwargs)
            domain = bound.arguments.get("domain")
            if domain:
                Ledger(runs_dir() / _clean(domain)).append({
                    "kind": "tool_output",
                    "tool": fn.__name__,
                    "tokens": estimate_tokens(json.dumps(result, ensure_ascii=False,
                                                         default=str)),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
        except Exception:
            pass                         # metering must never break a tool
        return result
    return mcp.tool()(wrapper)


def _run_dir(domain: str) -> Path:
    return runs_dir() / _clean(domain)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------- one call

def _job_reply(domain: str, current: dict) -> dict:
    """What the agent sees about a background run — the same shape at every stage."""
    state = current["state"]
    if state == "done":
        result = current["result"]
        scan = result["scan"]
        return {
            "state": "done",
            "report_to_user": result["report_to_user"],
            "next_step": result["next_step"],
            "devices": result["devices"],
            "held_for_review": result["held_for_review"],
            "pages_awaiting_judgment": result["pages_awaiting_judgment"],
            "reasoning_backend": scan["reasoning_backend"],
            "devices_csv": result["artifacts"]["devices_csv"],
        }
    if state == "running":
        p = current.get("progress") or {}
        return {
            "state": "running",
            "stage": p.get("stage", "starting"),
            "detail": p.get("detail", ""),
            "elapsed_s": current.get("elapsed_s"),
            "next_step": f"call catalogue_status('{domain}') — it waits up to 40 s for "
                         f"news. Tell the user the stage in one short line; do not "
                         f"estimate anything.",
        }
    if state in ("failed", "stalled"):
        return {
            "state": state,
            "log_tail": current.get("log_tail", ""),
            "next_step": "tell the user the run " + state + " and quote the last log lines; "
                         "calling catalogue_vendor again resumes from the cache",
        }
    return {"state": "idle", "next_step": f"call catalogue_vendor('{domain}') to start"}


@metered
def catalogue_vendor(
    domain: str,
    manufacturer: str | None = None,
    llm: str | None = None,
    budget_usd: float = 1.0,
    limit: int = 0,
) -> dict:
    """START HERE. Start cataloguing one vendor in the background.

    Discovery, scope, focused crawl, classification, extraction, datasheet PDFs, review
    and the automation screen run as a separate process, because a full run takes
    minutes and a tool call must return within about a minute. This call starts it (or
    reattaches to a run already going), waits up to 40 s, and returns its state.

    While `state` is "running", call `catalogue_status(domain)` until it is "done".
    When done, relay `report_to_user` verbatim — it carries the measured cost. Do not
    add your own estimate. Everything is cached: starting again after a failure resumes.

    llm: "auto" (default) | "anthropic" | "claude-cli" | "agent" (never call a model).
    budget_usd: hard cap on model spend for this run.
    """
    from . import jobs

    domain = _clean(domain)
    jobs.start(domain, manufacturer=manufacturer, llm=llm, budget_usd=budget_usd,
               limit=limit or None)
    return _job_reply(domain, jobs.wait(domain))


@metered
def catalogue_status(domain: str) -> dict:
    """Progress of a background run started by `catalogue_vendor`; the report when done.

    Waits up to 40 s for the run to finish before answering, so calling it in a loop is
    cheap. Returns `state` ("running" with the current stage, "done" with
    `report_to_user`, or "failed"/"stalled" with the log tail) and `next_step`.
    """
    from . import jobs

    domain = _clean(domain)
    return _job_reply(domain, jobs.wait(domain))


# --------------------------------------------------------------------------- stage 0-1

@metered
def site_overview(domain: str, delay: float = 0.25) -> dict:
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


@metered
def get_recipe(domain: str) -> dict:
    """Read the saved per-vendor recipe, or report that none exists."""
    recipe = load_recipe(domain)
    return recipe.to_dict() if recipe else {"domain": domain, "exists": False}


@metered
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

@metered
def scan_site(domain: str, limit: int = 0, delay: float = 0.25) -> dict:
    """Shortlist candidate product pages and classify them deterministically.

    `limit=0` means no cap. Fetches are cached, so re-running is fast and free. Returns
    counts and artifact paths — the per-page results stay on disk.
    """
    return run_scan(domain, limit=None if limit == 0 else limit, delay=delay)


@metered
def pending_classifications(domain: str, limit: int = 20) -> dict:
    """Pages the structural signals could not decide — your tier-B queue.

    Returns a compact digest per page (a few hundred tokens each), never the page. Judge
    each one and send the verdicts to `record_classifications`.

    Labels: instrument, accessory, consumable, software, service, category_page, other.
    Only `instrument` proceeds to extraction.
    """
    from .judge import CLASSIFY_SYSTEM

    domain = _clean(domain)
    out = _run_dir(domain)
    cache = Cache(cache_dir() / domain)
    store = VerdictStore.load(out)

    rows = [
        r for r in _read_jsonl(out / "classified.jsonl")
        if r["label"] == "unknown" and store.classification_for(r["url"]) is None
    ]
    digests = []
    for row in rows[:limit]:
        hit = cache.get(row["url"])
        if hit is not None:
            digests.append(digest_for_row(row, hit[1]))
    return {
        "domain": domain,
        "pending_total": len(rows),
        "returned": len(digests),
        "instructions": CLASSIFY_SYSTEM,
        "digests": digests,
        "next": "record_classifications with {url, label, reason} per digest",
    }


@metered
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

@metered
def extract_devices(domain: str, manufacturer: str | None = None) -> dict:
    """Build the device table from a completed scan. Fully offline.

    One row per device; variants that differ only in non-functional attributes (mains
    voltage, fuse rating, article number, bundled software) are merged. Writes
    `devices.csv`, `specs_eav.csv` and `review_queue.csv`.
    """
    return run_extract(domain, manufacturer=manufacturer)


@metered
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


@metered
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


@metered
def device_table(
    domain: str,
    limit: int = 25,
    category: str | None = None,
    include_specs: bool = False,
) -> dict:
    """The finished table — `devices.csv`, in its real columns. This is the deliverable.

    Present these rows in these columns. The column set is fixed and identical for every
    vendor and every run, so a reader can diff two runs; do not reorder it or substitute
    a column set of your own.

    `specs` is summarised as a key count by default, because the full bags cost about
    13,000 tokens for 25 rows and are unreadable in chat regardless. Set
    `include_specs=True` when the specifications themselves are the question, or use
    `device_specs` for one device. The CSV at `csv_path` always holds the full bags.
    """
    from .export import CORE_COLUMNS

    rows = table_records(_run_dir(domain))
    if category:
        rows = [r for r in rows if r["category"] == category]

    out = []
    for r in rows[:limit]:
        row = {
            "product_id": r["product_id"],
            "manufacturer": r["manufacturer"],
            "name": r["name"],
            "category": r["category"],
            "url": r["url"],
            "interfaces": "; ".join(r["interfaces"]),
            "description": r["description"][:160],
            "image_url": r["image_url"],
            "datasheet_urls": "; ".join(r["datasheet_urls"][:2]),
        }
        row["specs"] = (
            json.dumps({k: v.get("raw") for k, v in r["specs"].items()}, ensure_ascii=False)
            if include_specs
            else f"{len(r['specs'])} keys"
        )
        out.append(row)

    return {
        "columns": list(CORE_COLUMNS),
        "total_devices": len(rows),
        "returned": len(out),
        "categories": sorted({r["category"] for r in rows}),
        "csv_path": str(_run_dir(domain) / "devices.csv"),
        "specs_included": include_specs,
        "rows": out,
    }


@metered
def open_device_table(domain: str, open_browser: bool = True) -> dict:
    """Open `devices.csv` as a browsable, sortable, searchable page. **The deliverable.**

    Use this instead of printing rows into the conversation. It renders the same data as
    `devices.csv` — filter by name, category, interface or specification, sort any
    column, expand a device's full spec bag — and opens it in the default browser.

    Costs almost no tokens: the table goes to the screen, not through the context.
    Report the row count and the path, and stop there.
    """
    from .view import write_and_open

    out = _run_dir(domain)
    records = table_records(out)
    if not records:
        return {"error": f"no device table for {domain}; run catalogue_vendor first"}

    path, opened = write_and_open(
        records,
        domain=domain,
        csv_path=str(out / "devices.csv"),
        out_path=out / "devices.html",
        open_it=open_browser,
    )
    return {
        "devices": len(records),
        "view": str(path),
        "opened_in_browser": opened,
        "csv": str(out / "devices.csv"),
        "note": (
            "opened in the default browser" if opened
            else "written but not opened — open the path above manually"
        ),
    }


@metered
def device_specs(domain: str, name: str) -> dict:
    """Every specification of one device, with raw source text preserved."""
    for row in table_records(_run_dir(domain)):
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


@metered
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
            for r in table_records(out)
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


@metered
def estimate_cost(domain: str) -> dict:
    """What a vendor is likely to cost in model calls, from measured history.

    Not a formula: the median and range of what previous vendors in this installation
    actually spent, read from their ledgers. With no history it says so rather than
    guessing. After the run, `cost_report` gives the measured figure for this vendor.
    """
    est = calibrated_estimate(runs_dir(), exclude=_clean(domain))
    if est is None:
        return {"domain": domain, "estimate": None,
                "note": "no previous runs with model usage — no basis for an estimate yet"}
    return {
        "domain": domain,
        "estimate": est,
        "summary": (f"Previous vendors cost ${est['median_usd']:.2f} median "
                    f"(range ${est['min_usd']:.2f}-${est['max_usd']:.2f}) in model calls; "
                    f"basis: {est['basis']}."),
    }


@metered
def cost_report(domain: str) -> dict:
    """What this vendor actually cost — every model call and tool payload, measured.

    Reads the vendor's ledger: model calls made inside the pipeline (exact usage from
    the API or CLI) and the size of every tool result handed to the driving agent. The
    agent's own reasoning is the only part not counted, and the summary says so.
    """
    totals = Ledger(_run_dir(domain)).totals()
    parts = []
    if totals["model_calls"]:
        parts.append(
            f"{totals['model_calls']} model calls, {totals['input_tokens']:,} input / "
            f"{totals['output_tokens']:,} output tokens, ${totals['cost_usd']:.3f} "
            f"({', '.join(totals['models'])} via {', '.join(totals['backends'])})"
        )
    else:
        parts.append("no model calls inside the pipeline")
    if totals["tool_payload_tokens"]:
        parts.append(f"~{totals['tool_payload_tokens']:,} tokens of tool results over "
                     f"{totals['tool_calls']} tool calls")
    return {
        "domain": domain,
        "totals": totals,
        "summary": "; ".join(parts) + ". Excludes the agent's own conversation context.",
    }


@mcp.tool()
def get_procedure() -> dict:
    """The current step-by-step procedure for driving these tools.

    Read this first if you are not already following it. It ships inside the installed
    package, so it always matches the tools you actually have — unlike a copy uploaded
    separately, which silently goes stale when the server is updated.
    """
    path = Path(__file__).resolve().parent / "SKILL.md"
    if not path.exists():
        return {"error": "SKILL.md missing from the installed package"}
    return {"source": str(path), "procedure": path.read_text(encoding="utf-8")}


@mcp.tool()
def storage_paths() -> dict:
    """Where runs, caches and recipes are read from and written to.

    Set PRODSCRAPE_HOME to relocate them; inside a source checkout they stay in the
    checkout.
    """
    return describe()


@metered
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
        "devices": len(table_records(out)),
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
