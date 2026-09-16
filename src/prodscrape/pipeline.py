"""Run orchestration for stages 0-2, writing the artifacts a run is judged on.

Every stage persists a JSONL file under ``runs/<domain>/``. Re-running reads the HTTP
cache, so a second run over the same vendor costs nothing and produces identical output.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .discover import discover_site
from .fetch import Cache, Fetcher
from .inventory import prefix_tree, render_tree, select_candidates, url_depth
from .recipes import Recipe, infer_rules, load_recipe
from .signals import classify_by_signals, page_signals
from .verdicts import VerdictStore

# Resolved per call so PRODSCRAPE_HOME can be set after import (and so tests can
# redirect it); see paths.py.
from .paths import cache_dir, runs_dir


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _summary_md(domain: str, recipe: Recipe, verdicts: list[dict], capped: int | None) -> str:
    by_label: dict[str, list[dict]] = {}
    for v in verdicts:
        by_label.setdefault(v["label"], []).append(v)

    lines = [
        f"# Classification results — {domain}",
        "",
        f"Recipe: {'INFERRED (unverified)' if recipe.inferred else 'saved recipe'}",
        f"Include: `{recipe.include}`  ·  family_depth: `{recipe.family_depth}`",
        "",
    ]
    if recipe.notes:
        lines += ["Inference notes:", ""] + [f"- {n}" for n in recipe.notes] + [""]
    if capped:
        lines += [f"> Capped at {capped} pages for this run (`--limit`).", ""]

    lines += ["| label | count |", "|---|---|"]
    for label in sorted(by_label, key=lambda k: -len(by_label[k])):
        lines.append(f"| {label} | {len(by_label[label])} |")
    escalation = len(by_label.get("unknown", [])) / max(len(verdicts), 1)
    lines += ["", f"**Tier-B escalation rate: {escalation:.0%}** "
                  f"({len(by_label.get('unknown', []))}/{len(verdicts)} pages need a model call)", ""]

    for label in ("instrument", "unknown", "other"):
        rows = by_label.get(label, [])
        if not rows:
            continue
        lines += [f"## {label} ({len(rows)})", "", "| conf | page | why |", "|---|---|---|"]
        for v in sorted(rows, key=lambda r: -r["confidence"]):
            name = (v["signals"]["h1"] or v["signals"]["slug"])[:60]
            lines.append(
                f"| {v['confidence']:.2f} | {name} | {v['reason'][:90]} |"
            )
        lines.append("")
    return "\n".join(lines)


def run_scan(
    domain: str,
    *,
    limit: int | None = 40,
    delay: float = 1.0,
    out_dir: Path | None = None,
    cache_directory: Path | None = None,
) -> dict:
    """Stages 0-2 for one vendor. Returns a summary dict; writes artifacts to disk."""
    started = time.time()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    out = Path(out_dir or runs_dir() / domain)
    out.mkdir(parents=True, exist_ok=True)
    cache = Cache(cache_directory or cache_dir() / domain)

    with Fetcher(cache, delay=delay) as fetcher:
        # Stage 0 -------------------------------------------------------------
        profile = discover_site(domain, fetcher)
        urls = [u["url"] for u in profile.urls]
        _write_jsonl(out / "urls_raw.jsonl", profile.urls)

        # Stage 1 -------------------------------------------------------------
        tree = render_tree(prefix_tree(urls, max_depth=3))
        (out / "tree.txt").write_text(tree, encoding="utf-8")

        recipe = load_recipe(profile.domain) or load_recipe(domain)
        if recipe is None:
            recipe = infer_rules(urls, profile.domain)
        recipe.platform = recipe.platform or profile.platform
        recipe.base_url = recipe.base_url or profile.base_url

        candidates = select_candidates(
            urls,
            include=recipe.include or None,
            exclude=recipe.exclude or None,
            depth=recipe.family_depth,
        )
        _write_jsonl(
            out / "candidates.jsonl",
            [{"url": u, "depth": url_depth(u), "discovered_via": "sitemap"} for u in candidates],
        )

        capped = None
        fetch_list = candidates
        if limit is not None and len(candidates) > limit:
            capped = limit
            fetch_list = candidates[:limit]

        # Stage 2 -------------------------------------------------------------
        store = VerdictStore.load(out)
        verdicts: list[dict] = []
        for url in fetch_list:
            try:
                _, html = fetcher.get(url)
            except Exception as exc:  # network/robots failures are data, not crashes
                verdicts.append(
                    {"url": url, "label": "error", "confidence": 0.0,
                     "reason": f"{type(exc).__name__}: {exc}", "decided_by": "fetch",
                     "signals": {"h1": "", "slug": url.rstrip("/").rsplit("/", 1)[-1]}}
                )
                continue
            sig = page_signals(
                url,
                html,
                spec_headings=recipe.spec_headings,
                order_headings=recipe.order_headings,
            )
            verdict = classify_by_signals(
                sig,
                family_depth=recipe.family_depth,
                slug_suffix=recipe.slug_suffix,
                threshold=recipe.threshold,
            )
            row = asdict(verdict)
            row["signals"] = sig.as_dict()
            # A judgment already given is never re-asked: stored agent verdicts win over
            # the deterministic label, so re-runs stay free and stay consistent.
            stored = store.classification_for(url)
            if stored:
                row.update(
                    label=stored["label"],
                    reason=stored["reason"],
                    decided_by=stored["decided_by"],
                )
            verdicts.append(row)

    _write_jsonl(out / "classified.jsonl", verdicts)
    (out / "summary.md").write_text(
        _summary_md(domain, recipe, verdicts, capped), encoding="utf-8"
    )

    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["label"]] = counts.get(v["label"], 0) + 1

    manifest = {
        "domain": profile.domain,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_s": round(time.time() - started, 1),
        "platform": profile.platform,
        "sitemap_declared_in_robots": profile.sitemap_declared_in_robots,
        "robots_ok": profile.robots_ok,
        "discovery_errors": profile.errors,
        "sitemaps_found": profile.sitemaps_found,
        "total_urls": len(urls),
        "candidates": len(candidates),
        "classified": len(verdicts),
        "capped_at": capped,
        "agent_verdicts_applied": sum(
            1 for v in verdicts if v.get("decided_by") == "model"
        ),
        "recipe_inferred": recipe.inferred,
        "recipe_notes": recipe.notes,
        "recipe": recipe.to_dict(),
        "label_counts": counts,
        "cache_entries": len(cache),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def run_extract(
    domain: str,
    *,
    manufacturer: str | None = None,
    out_dir: Path | None = None,
    cache_directory: Path | None = None,
) -> dict:
    """Stage 3-4 for one vendor, reading the Stage 2 verdicts and the HTTP cache.

    Runs entirely offline: every page it needs was already fetched during the scan.
    """
    from .export import (
        CORE_COLUMNS, is_device_row, to_eav, to_review_queue, to_wide,
        write_csv, write_jsonl,
    )
    from .extract import extract_page

    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    out = Path(out_dir or runs_dir() / domain)
    cache = Cache(cache_directory or cache_dir() / domain)
    recipe = load_recipe(domain) or load_recipe(f"www.{domain}")
    vendor = manufacturer or domain.split(".")[0].replace("-", " ").title()

    classified_path = out / "classified.jsonl"
    if not classified_path.exists():
        raise FileNotFoundError(f"no scan found at {classified_path}; run `scan` first")

    records = []
    missing = 0
    for line in classified_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["label"] != "instrument":
            continue
        hit = cache.get(row["url"])
        if hit is None:
            missing += 1
            continue
        records.extend(
            extract_page(row["url"], hit[1], manufacturer=vendor, recipe=recipe)
        )

    # Zero specs means no table on the page passed validation — in practice a series or
    # overview page. Strong indicator, not proof, so these are held for review rather than
    # dropped: a real device documented without a table must stay visible.
    store = VerdictStore.load(out)

    def accepted(record) -> bool:
        decided = store.review_for(record.product_id)
        if decided:
            return decided["verdict"] == "device"
        return is_device_row(record)

    devices = [r for r in records if accepted(r)]
    review = [
        r for r in records
        if not accepted(r) and store.review_for(r.product_id) is None
    ]

    write_jsonl(records, out / "extracted.jsonl")          # lossless: everything
    write_csv(to_wide(devices), out / "devices.csv", list(CORE_COLUMNS))
    write_csv(to_eav(devices), out / "specs_eav.csv")
    write_csv(to_review_queue(review), out / "review_queue.csv")

    with_specs = len(devices)
    merged = sum(1 for r in devices if len(r.source_variants) > 1)
    summary = {
        "domain": domain,
        "manufacturer": vendor,
        "pages_extracted": len({r.url for r in records}),
        "devices": len(devices),
        "held_for_review": len(review),
        "review_verdicts_applied": store.counts["reviews"],
        "records_total": len(records),
        "merged_variant_groups": merged,
        "pages_missing_from_cache": missing,
        "distinct_attributes": len({k for r in devices for k in r.specs}),
        "devices_with_interfaces": sum(1 for r in devices if r.interfaces),
    }
    (out / "extract_manifest.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary
