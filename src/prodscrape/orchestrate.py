"""One call, whole vendor: scan, judge, extract, review, report.

The skill used to be a ten-step procedure the agent had to follow in order, and in
practice it did not: runs in Claude chat skipped the cost report, or replaced the
measured figure with an estimate of its own. The fix is structural rather than a firmer
instruction — the steps that must always happen now happen in code, and the one
message the user must see is composed here, from measured numbers, for the agent to
relay verbatim.

``catalogue`` runs to completion when a reasoning backend is available (see
``llm.py``). Without one it runs every deterministic stage, leaves the ambiguous pages
and records queued, and says exactly which tool to call next.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .fetch import DEFAULT_DELAY
from .llm import DEFAULT_BUDGET_USD, Ledger, Reasoner, resolve_backend
from .paths import runs_dir
from .pipeline import run_extract, run_scan
from .verdicts import VerdictStore


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def review_rows(out: Path) -> list[dict]:
    """No-spec records still awaiting a device / not-a-device verdict."""
    store = VerdictStore.load(out)
    return [
        {
            "product_id": r["product_id"],
            "name": r["name"],
            "category": r["category"],
            "url": r["url"],
            "description": (r.get("description") or "")[:300],
            "interfaces": r.get("interfaces", []),
            "datasheet_count": len(r.get("datasheet_urls", [])),
        }
        for r in _read_jsonl(out / "extracted.jsonl")
        if not r["specs"] and store.review_for(r["product_id"]) is None
    ]


def catalogue(
    domain: str,
    *,
    manufacturer: str | None = None,
    llm: str | None = None,
    budget_usd: float = DEFAULT_BUDGET_USD,
    limit: int | None = None,
    delay: float = DEFAULT_DELAY,
    pdfs: bool = True,
) -> dict:
    from .judge import review_records
    from .pdfs import enrich_with_datasheets

    started = time.time()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    out = runs_dir() / domain
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out)
    spent_before = ledger.spent_usd()
    reasoner = Reasoner(resolve_backend(llm), ledger, budget_usd + spent_before)

    scan = run_scan(domain, limit=limit, delay=delay, reasoner=reasoner)
    extract = run_extract(domain, manufacturer=manufacturer)

    pdf_summary = None
    if pdfs:
        pdf_summary = enrich_with_datasheets(domain, delay=delay)
        if pdf_summary.get("devices_enriched") or pdf_summary.get("records_recovered"):
            extract = run_extract(domain, manufacturer=manufacturer)

    review = None
    pending_review = review_rows(out)
    if pending_review and reasoner.available:
        review = review_records(reasoner, pending_review, VerdictStore.load(out))
        extract = run_extract(domain, manufacturer=manufacturer)

    # Last judgment: could each device be part of an automated lab? Lenient — only a
    # clear "no" leaves the table, into excluded.csv.
    relevance = None
    if reasoner.available:
        from .judge import relevance_rows, screen_relevance

        store = VerdictStore.load(out)
        accepted_ids = _device_ids(out)
        rows = [r for r in relevance_rows(_read_jsonl(out / "extracted.jsonl"), store)
                if r["product_id"] in accepted_ids]
        if rows:
            relevance = screen_relevance(reasoner, rows, store)
            extract = run_extract(domain, manufacturer=manufacturer)

    result = {
        "domain": domain,
        "manufacturer": extract["manufacturer"],
        "devices": extract["devices"],
        "held_for_review": extract["held_for_review"],
        "pages_awaiting_judgment": scan["pending_for_agent"],
        "scan": scan,
        "extract": extract,
        "datasheets": pdf_summary,
        "model_review": review,
        "relevance": relevance,
        "excluded_not_automatable": extract.get("excluded_not_automatable", 0),
        "duration_s": round(time.time() - started, 1),
        "artifacts": {
            "devices_csv": str(out / "devices.csv"),
            "specs_eav_csv": str(out / "specs_eav.csv"),
            "review_queue_csv": str(out / "review_queue.csv"),
            "ledger": str(ledger.path),
            "report": str(out / "report.md"),
        },
    }
    result["usage"] = ledger.totals()
    result["run_usage_usd"] = round(ledger.spent_usd() - spent_before, 4)
    result["report_to_user"] = compose_report(result)
    result["next_step"] = next_step(result)
    (out / "report.md").write_text(result["report_to_user"], encoding="utf-8")
    (out / "catalogue_manifest.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def _device_ids(out: Path) -> set[str]:
    import csv

    path = out / "devices.csv"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig") as fh:
        return {row["product_id"] for row in csv.DictReader(fh)}


def next_step(result: dict) -> str:
    if result["pages_awaiting_judgment"]:
        return ("call pending_classifications, judge each digest, send the verdicts to "
                "record_classifications, then call catalogue_vendor again")
    held = result["held_for_review"]
    if held and not result.get("model_review"):
        return ("call pending_reviews, judge each row, send the verdicts to "
                "record_reviews, then call catalogue_vendor again")
    return "done — call open_device_table and relay report_to_user verbatim"


def compose_report(r: dict) -> str:
    """The message the user sees. Built from measured numbers only."""
    scan, usage = r["scan"], r["usage"]
    lines = [
        f"**{r['manufacturer']}** — {r['devices']} devices in `devices.csv`"
        f"{f', {r['held_for_review']} held for review' if r['held_for_review'] else ''}"
        f"{f', {r['pages_awaiting_judgment']} pages awaiting judgment' if r['pages_awaiting_judgment'] else ''}.",
    ]

    if usage["model_calls"]:
        stages = ", ".join(
            f"{name} {s['calls']}×" for name, s in usage["by_stage"].items()
        )
        lines.append(
            f"Model usage (measured, whole vendor to date): {usage['model_calls']} calls "
            f"({stages}) on {', '.join(usage['models'])} via {', '.join(usage['backends'])} — "
            f"{usage['input_tokens']:,} input / {usage['output_tokens']:,} output tokens, "
            f"${usage['cost_usd']:.3f}. This run: ${r['run_usage_usd']:.3f}."
        )
        if "claude-cli" in usage["backends"]:
            lines.append(
                "The $ figure is the list-price equivalent reported by the `claude` CLI. "
                "On a Claude subscription (Pro/Max) these calls count against your plan's "
                "usage limits and are not billed per token."
            )
    else:
        lines.append("Model usage: none — every decision came from deterministic rules "
                     "or a saved recipe.")
    if usage["tool_payload_tokens"]:
        lines.append(
            f"Tool results passed to the agent's context: ~{usage['tool_payload_tokens']:,} "
            f"tokens over {usage['tool_calls']} tool calls."
        )
    lines.append("Not included: the driving agent's own conversation context.")

    sc = scan["scope"]
    scope_line = (
        f"Scope: {sc['decided_by']} — {sc['products_named']} products and "
        f"{sc['hubs_named']} category pages named from the vendor's menu"
    )
    if len(sc["hosts"]) > 1:
        scope_line += f", across hosts {', '.join(sc['hosts'])}"
    if sc["affiliated_domains"]:
        scope_line += f"; also catalogues on {', '.join(sc['affiliated_domains'])}"
    lines.append(scope_line + ".")

    caveats = []
    if sc["decided_by"] == "heuristic":
        caveats.append("scope came from a keyword heuristic (no model available) — "
                       "recall may be low")
    if scan.get("pages_from_archive"):
        caveats.append(f"{scan['pages_from_archive']} pages came from the Internet "
                       f"Archive because the live site blocks automated access")
    if scan.get("blocked") and not scan.get("pages_from_archive"):
        caveats.append("the site blocks automated access and no archive copy was found")
    if scan.get("unvisited_candidates"):
        caveats.append(f"{scan['unvisited_candidates']} candidate pages not visited "
                       f"(page budget reached)")
    ds = r.get("datasheets") or {}
    if ds.get("devices_enriched") or ds.get("records_recovered"):
        caveats.append(f"datasheet PDFs added specs to {ds.get('devices_enriched', 0)} "
                       f"devices")
    if r.get("excluded_not_automatable"):
        caveats.append(f"{r['excluded_not_automatable']} devices judged not usable in an "
                       f"automated lab were moved to excluded.csv (with reasons)")
    rv = r.get("model_review") or {}
    if rv.get("stopped"):
        caveats.append(f"review stopped: {rv['stopped']}")
    for c in caveats:
        lines.append(f"- {c}")
    lines.append(f"Table: {r['artifacts']['devices_csv']}")
    return "\n".join(lines)
