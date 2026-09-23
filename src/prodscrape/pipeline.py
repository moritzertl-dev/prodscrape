"""Run orchestration for stages 0-2, writing the artifacts a run is judged on.

Every stage persists a JSONL file under ``runs/<domain>/``. Re-running reads the HTTP
cache, so a second run over the same vendor costs nothing and produces identical output.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from .discover import discover_site
from .fetch import DEFAULT_DELAY, Cache, Fetcher
from .inventory import (
    collapse_query_variants, prefix_tree, render_tree, select_candidates, url_depth,
)
from urllib.parse import urlparse

from .digest import digest_for_row
from .extract import product_sections
from .harvest import Candidate, Frontier, product_link_count, sitemap_candidates
from .judge import classify_pages, triage_branches, triage_links
from .llm import (
    DEFAULT_BUDGET_USD, BudgetExceeded, Ledger, LLMUnavailable, Reasoner, resolve_backend,
)
from .navigation import NavEntry, registrable_domain
from .recipes import Recipe, infer_rules, load_recipe, save_recipe
from .scope import (
    ScopeDecision, SiteContext, decide_heuristically, decide_with_model, gather_context,
    render_digest,
)
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


def _page_key(url: str) -> str:
    p = urlparse(url)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


SITEMAP_TRIAGE_OVER = 40   # sitemap candidates beyond this are triaged, not all fetched


def _slug_words(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", slug)[:80]


PREFETCH = 24             # candidates fetched in parallel ahead of the loop
HUB_MIN_LINKS = 8          # content links that make an unlabelled page a listing page
DEFAULT_MAX_PAGES = 400
DEFAULT_MAX_DEPTH = 2
PRIORS = {
    "nav": (0.35, "named as an instrument in the vendor's menu"),
    "hub": (0.10, "listed on a catalogue page"),
    "sitemap": (0.05, "under a catalogue branch"),
    "recipe": (0.0, ""),
}


def _decide_scope(
    domain: str, profile, fetcher: Fetcher, urls: list[str], reasoner: Reasoner,
) -> tuple[ScopeDecision, SiteContext]:
    ctx = gather_context(domain, profile.base_url, fetcher, urls)
    decision: ScopeDecision | None = None
    if reasoner.available and ctx.entries:
        try:
            decision = decide_with_model(ctx, reasoner)
        except (LLMUnavailable, BudgetExceeded, ValueError, RuntimeError) as exc:
            ctx.notes.append(f"model scope failed ({exc}); used the heuristic")
    if decision is None:
        guess = infer_rules(urls, profile.domain)
        prefixes = [p.rstrip("*") for p in guess.include]
        decision = decide_heuristically(ctx, prefixes)
    decision.notes = ctx.notes + decision.notes
    return decision, ctx


def _affiliated_round(
    frontier: Frontier, decision: ScopeDecision, fetcher: Fetcher, reasoner: Reasoner,
    vendor_domain: str, max_domains: int = 3,
) -> list[str]:
    """Second catalogues on other domains, linked from the vendor's own hubs.

    Only a model decides these: whether brookslabautomation.com is Brooks' own catalogue
    or a distributor is a judgment, and the heuristic has no business making it.
    """
    if not reasoner.available:
        return []
    by_domain: dict[str, list] = {}
    vendor_reg = registrable_domain(vendor_domain)
    # External sites the scope step chose straight from the menu come first — the model
    # already judged them part of the range; the round reads their own menus.
    for e in decision.products + decision.hubs:
        dom = registrable_domain(urlparse(e.url).netloc)
        if dom != vendor_reg:
            by_domain.setdefault(dom, []).append(NavEntry(text=e.name, url=e.url))
    for link in frontier.affiliated_links:
        dom = registrable_domain(urlparse(link.url).netloc)
        if dom != vendor_reg:
            by_domain.setdefault(dom, []).append(link)
    accepted: list[str] = []
    for dom, links in list(by_domain.items())[:max_domains]:
        home = links[0].url
        base = f"{urlparse(home).scheme}://{urlparse(home).netloc}"
        ctx = gather_context(dom, base, fetcher, [])
        if not ctx.entries:
            continue
        anchor = "; ".join(sorted({l.text for l in links if l.text})[:3])
        preamble = (
            f"This is a SEPARATE website, {dom}, linked from {vendor_domain}'s catalogue "
            f"pages (link text: {anchor!r}). Select entries only if this site presents "
            f"instruments sold by {vendor_domain} or its own brands; if it is a "
            f"distributor, partner or unrelated site, return empty lists."
        )
        try:
            extra = decide_with_model(ctx, reasoner, preamble=preamble)
        except (LLMUnavailable, BudgetExceeded, ValueError, RuntimeError):
            continue
        if extra.products or extra.hubs:
            accepted.append(dom)
            decision.merge(extra)
            decision.hosts = sorted(set(decision.hosts) | {urlparse(base).netloc})
            for e in extra.products:
                frontier.push("page", Candidate(url=e.url, name=e.name,
                                                category=e.category, via="nav"))
            for e in extra.hubs:
                frontier.push("hub", Candidate(url=e.url, name=e.name,
                                               category=e.category or e.name, via="nav"))
    frontier.affiliated_links = []
    return accepted


def run_scan(
    domain: str,
    *,
    limit: int | None = None,
    delay: float = DEFAULT_DELAY,
    out_dir: Path | None = None,
    cache_directory: Path | None = None,
    reasoner: Reasoner | None = None,
    llm: str | None = None,
    budget_usd: float | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict:
    """Stages 0-2 for one vendor. Returns a summary dict; writes artifacts to disk.

    With a reasoning backend, scope and the ambiguous minority are judged inline and the
    run needs no agent at all. Without one, the ambiguous pages stay ``unknown`` for the
    agent's ``pending_classifications`` queue.
    """
    started = time.time()
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    out = Path(out_dir or runs_dir() / domain)
    out.mkdir(parents=True, exist_ok=True)
    cache = Cache(cache_directory or cache_dir() / domain)
    ledger = Ledger(out)
    if reasoner is None:
        reasoner = Reasoner(resolve_backend(llm), ledger,
                            budget_usd if budget_usd is not None else DEFAULT_BUDGET_USD)
    max_pages = limit or DEFAULT_MAX_PAGES

    with Fetcher(cache, delay=delay) as fetcher:
        # Stage 0 -------------------------------------------------------------
        profile = discover_site(domain, fetcher)
        urls = [u["url"] for u in profile.urls]
        _write_jsonl(out / "urls_raw.jsonl", profile.urls)
        (out / "tree.txt").write_text(render_tree(prefix_tree(urls, max_depth=3)),
                                      encoding="utf-8")

        # Stage 1: scope --------------------------------------------------------
        recipe = load_recipe(profile.domain) or load_recipe(domain)
        menu: list = []
        fresh_scope = False
        if recipe is not None and recipe.scope:
            decision = ScopeDecision.from_dict(recipe.scope)
            decision.notes.append(f"scope replayed from recipe (originally "
                                  f"{recipe.scope.get('decided_by', '?')})")
        elif recipe is not None and recipe.include:
            # A recipe written before scopes existed: its include rules select.
            decision = ScopeDecision(decided_by="recipe",
                                     notes=["legacy recipe: include rules select candidates"])
        else:
            decision, ctx = _decide_scope(domain, profile, fetcher, urls, reasoner)
            menu = ctx.entries
            fresh_scope = True
            (out / "site_digest.txt").write_text(render_digest(ctx), encoding="utf-8")

        # Links are triaged before they are fetched: by the model when one is available
        # (decisions persisted after every batch, so replays and resumed runs are free),
        # otherwise all of them are kept and the page budget bounds the crawl.
        triage_path = out / "triage.json"
        known = json.loads(triage_path.read_text(encoding="utf-8")) if triage_path.exists() else {}
        triage_stats = {"links": 0, "kept": 0}

        def _save_triage() -> None:
            triage_path.write_text(json.dumps(known, indent=1, ensure_ascii=False),
                                   encoding="utf-8")

        base_host = urlparse(profile.base_url).netloc
        frontier = Frontier(decision, menu, domain, base_host)
        if recipe is not None and recipe.include and not recipe.scope:
            legacy = select_candidates(
                urls, include=recipe.include or None, exclude=recipe.exclude or None,
                depths=recipe.family_depths or None, leaf_only=recipe.leaf_only,
            )
            legacy, _ = collapse_query_variants(legacy)
            frontier.seed([Candidate(url=u, via="recipe") for u in legacy])
        else:
            sitemap_c = sitemap_candidates(urls, decision, base_host)
            canonical, _ = collapse_query_variants([c.url for c in sitemap_c])
            keep = set(canonical)
            sitemap_c = [c for c in sitemap_c if c.url in keep]
            frontier.seed([])
            # A large catalogue branch mixes instruments with columns, parts and
            # supplies (agilent.com: ~4,000 leaves under /en/product/). Its URLs go
            # through the same triage as hub links; the slug alone
            # ("8890b-gc-system" vs "hp-5ms-gc-column") usually settles it.
            if reasoner.available and len(sitemap_c) > SITEMAP_TRIAGE_OVER:
                n_branches = len({urlparse(c.url).path.rstrip("/").rsplit("/", 1)[0]
                                  for c in sitemap_c})
                if n_branches >= 5 and len(sitemap_c) > 3 * n_branches:
                    sitemap_c = triage_branches(reasoner, sitemap_c, known)
                    _save_triage()
                for c in sitemap_c:
                    c.name = c.name or _slug_words(c.url)
                    if frontier.push("page", c):
                        frontier.items.pop()
                        frontier.pending.append(c)
            else:
                for c in sitemap_c:
                    frontier.push("page", c)
        allowed_hosts = set(decision.hosts) | {base_host}
        for dom in (recipe.affiliated_domains if recipe else []):
            allowed_hosts |= {dom, f"www.{dom}"}

        # Stage 1b + 2: focused crawl and deterministic classification ----------
        store = VerdictStore.load(out)
        rows: list[dict] = []
        fetched = 0
        seen_final: set[str] = set()
        duplicates: list[str] = []
        affiliated: list[str] = list(recipe.affiliated_domains) if recipe else []

        def drain(position: int) -> int:
            nonlocal fetched
            while position < len(frontier.items) and fetched < max_pages:
                # Fetch the next few candidates in parallel; the loop below then
                # reads them from the cache in order, so results stay deterministic.
                if position % PREFETCH == 0:
                    ahead = frontier.items[position:position + PREFETCH]
                    fetcher.prefetch([c.url for _, c in ahead][: max_pages - fetched])
                kind, cand = frontier.items[position]
                position += 1
                reached = {"via": cand.via, "name": cand.name, "category": cand.category,
                           "parent": cand.parent, "depth": cand.depth}
                slug = cand.url.rstrip("/").rsplit("/", 1)[-1]
                try:
                    rec, html = fetcher.get(cand.url)
                    fetched += 1
                    if rec.status >= 400:
                        raise RuntimeError(f"HTTP {rec.status}")
                except Exception as exc:
                    rows.append({"url": cand.url, "label": "error", "confidence": 0.0,
                                 "reason": f"{type(exc).__name__}: {exc}",
                                 "decided_by": "fetch", "reached": reached,
                                 "signals": {"h1": "", "slug": slug}})
                    continue
                # Sitemaps keep old slugs that redirect to the current page
                # (Formulatrix: .../mantis-liquid-handler/ -> .../mantis-liquid-dispenser/).
                # One page, one row — the first way it was reached wins.
                final_key = _page_key(rec.final_url or cand.url)
                if final_key in seen_final:
                    duplicates.append(cand.url)
                    continue
                seen_final.add(final_key)

                sig = page_signals(
                    cand.url, html,
                    spec_headings=recipe.spec_headings if recipe else None,
                    order_headings=recipe.order_headings if recipe else None,
                )
                if kind == "hub":
                    added = frontier.expand(html, cand, allowed_hosts)
                    row = {"url": cand.url, "label": "category_page", "confidence": 1.0,
                           "reason": f"catalogue hub from scope; {added} links queued",
                           "decided_by": "scope"}
                else:
                    prior, why = PRIORS.get(cand.via, (0.0, ""))
                    verdict = classify_by_signals(
                        sig,
                        family_depth=recipe.family_depth if recipe else None,
                        slug_suffix=recipe.slug_suffix if recipe else None,
                        threshold=recipe.threshold if recipe else 0.6,
                        prior=prior, prior_reason=why,
                    )
                    row = asdict(verdict)
                    # A page the scope step named as a product is re-read as a listing
                    # only when a model triages what it links to. Beckman keeps specs on
                    # per-model pages under a family page ("Explore Microfuge 20
                    # Models"), but Tecan's Fluent page links 56 things, mostly
                    # citations and brochures — untriaged, that flooded the crawl.
                    if (row["label"] != "instrument" and not sig.has_spec_heading
                            and (cand.via != "nav" or reasoner.available)
                            and cand.depth < max_depth):
                        n = product_link_count(html, cand.url, domain)
                        if n >= HUB_MIN_LINKS:
                            # Follow its links, but do not relabel it: Azenta's product
                            # pages list "related products", and calling them listing
                            # pages dropped nine storage systems. The page itself is
                            # still judged on its own evidence.
                            added = frontier.expand(html, cand, allowed_hosts)
                            row["reason"] += (f"; links like a listing page ({n}), "
                                              f"{added} new queued")
                # Several products presented as in-page sections, no page of their
                # own (PreciseFlex robots): the page itself is the source.
                sections = product_sections(html, cand.url)
                if len(sections) >= 2 and row["label"] in ("category_page", "unknown", "other"):
                    row.update(label="instrument", decided_by="signals",
                               reason=f"multi-product page: {len(sections)} in-page "
                                      f"product sections")
                    row["sections"] = [name for name, _ in sections]
                row["signals"] = sig.as_dict()
                row["reached"] = reached
                row["source"] = rec.source
                stored = store.classification_for(cand.url)
                if stored:
                    row.update(label=stored["label"], reason=stored["reason"],
                               decided_by=stored["decided_by"])
                rows.append(row)
            return position

        def keep(cands):
            if reasoner.available:
                decisions = triage_links(reasoner, cands, known)
                _save_triage()
            else:
                decisions = [True] * len(cands)
            triage_stats["links"] += len(cands)
            triage_stats["kept"] += sum(decisions)
            return decisions

        pos = drain(0)
        external_chosen = any(
            registrable_domain(urlparse(e.url).netloc) != registrable_domain(domain)
            for e in decision.products + decision.hubs
        )
        affiliated_done = False
        while fetched < max_pages:
            if frontier.pending:
                frontier.release(keep)
            elif (fresh_scope and not affiliated_done
                  and (frontier.affiliated_links or external_chosen)):
                affiliated_done = True
                affiliated += _affiliated_round(frontier, decision, fetcher, reasoner, domain)
                allowed_hosts |= set(decision.hosts)
            if pos >= len(frontier.items) and not frontier.pending:
                break
            pos = drain(pos)
        _save_triage()
        unvisited = len(frontier.items) - pos + len(frontier.pending)

        # Stage 2 tier B: the ambiguous minority, judged inline when possible ----
        judged = None
        pending = [r for r in rows if r["label"] == "unknown"
                   and store.classification_for(r["url"]) is None]
        if pending and reasoner.available:
            digests = []
            for r in pending:
                hit = cache.get(r["url"])
                if hit:
                    digests.append(digest_for_row(r, hit[1]))
            judged = classify_pages(reasoner, digests, store)
            for r in rows:
                stored = store.classification_for(r["url"])
                if stored and r["label"] == "unknown":
                    r.update(label=stored["label"], reason=stored["reason"],
                             decided_by=stored["decided_by"])
        archive_hits = list(fetcher.archive_hits)

    if fresh_scope:
        new_recipe = recipe or Recipe(domain=profile.domain)
        new_recipe.scope = decision.as_dict()
        new_recipe.affiliated_domains = affiliated
        new_recipe.base_url = profile.base_url
        new_recipe.platform = profile.platform
        new_recipe.inferred = True
        save_recipe(new_recipe)

    _write_jsonl(out / "candidates.jsonl", [
        {"kind": k, **c.as_dict()} for k, c in frontier.items
    ])
    _write_jsonl(out / "classified.jsonl", rows)
    (out / "summary.md").write_text(
        _summary_md(domain, recipe or Recipe(domain=domain, inferred=True), rows,
                    max_pages if unvisited else None),
        encoding="utf-8",
    )

    counts: dict[str, int] = {}
    for v in rows:
        counts[v["label"]] = counts.get(v["label"], 0) + 1
    via_counts: dict[str, int] = {}
    for _, c in frontier.items:
        via_counts[c.via] = via_counts.get(c.via, 0) + 1

    manifest = {
        "domain": profile.domain,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_s": round(time.time() - started, 1),
        "platform": profile.platform,
        "sitemap_declared_in_robots": profile.sitemap_declared_in_robots,
        "robots_ok": profile.robots_ok,
        "blocked": profile.blocked,
        "discovery_errors": profile.errors,
        "sitemaps_found": profile.sitemaps_found,
        "total_urls": len(urls),
        "scope": {
            "decided_by": decision.decided_by,
            "products_named": len(decision.products),
            "hubs_named": len(decision.hubs),
            "catalogue_prefixes": decision.catalogue_prefixes,
            "hosts": decision.hosts,
            "affiliated_domains": affiliated,
            "notes": decision.notes,
        },
        "candidates": len(frontier.items),
        "candidates_by_source": via_counts,
        "pages_fetched": fetched,
        "hub_links_triaged": triage_stats["links"],
        "hub_links_kept": triage_stats["kept"],
        "redirect_duplicates_skipped": len(duplicates),
        "unvisited_candidates": unvisited,
        "pages_from_archive": sum(1 for r in rows if r.get("source") == "archive"),
        "classified": len(rows),
        "label_counts": counts,
        "model_judged": judged,
        "pending_for_agent": sum(1 for r in rows if r["label"] == "unknown"),
        "reasoning_backend": reasoner.backend.name if reasoner.backend else "agent",
        "recipe_saved": fresh_scope,
        "cache_entries": len(cache),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"[®™©]", "", name.lower()))


def _same_device(a, b) -> bool:
    """Whether two same-named records are one device, on the evidence.

    A shared name is not enough. Retsch titles every sieve shaker page with the generic
    "Vibrationssiebmaschine", and AS 200 and AS 300 are different machines. So: no
    shared spec may disagree, and the name must carry a model designation (a digit), or
    at least three shared specs must agree outright.
    """
    shared = set(a.specs) & set(b.specs)
    if any(a.specs[k].raw != b.specs[k].raw for k in shared):
        return False
    if a.url == b.url:
        return a.specs == b.specs
    return bool(re.search(r"\d", a.name)) or len(shared) >= 3


def merge_cross_page_duplicates(records: list) -> list:
    """One device listed on several pages is one row.

    PreciseFlex lists the same robots on its lab-automation page and again on its
    electronics-testing page; Formulatrix reaches Rock Imager from two menus. Records
    whose names are identical once case, spacing and trademark signs are ignored are
    merged: the one with more specs is kept, the other's specs fill its gaps, and the
    extra URL is noted. Names that merely resemble each other are never merged.
    """
    groups: dict[str, list] = {}
    order: list[str] = []
    for r in records:
        key = _name_key(r.name)
        if not key:
            key = r.product_id
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(r)
    out = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            out.append(group[0])
            continue
        group.sort(key=lambda r: -len(r.specs))
        keep = group[0]
        for other in group[1:]:
            if not _same_device(keep, other):
                out.append(other)
                continue
            for k, v in other.specs.items():
                keep.specs.setdefault(k, v)
            for iface in other.interfaces:
                if iface not in keep.interfaces:
                    keep.interfaces.append(iface)
            if other.url != keep.url:
                keep.warnings.append(f"also listed at {other.url}")
        out.append(keep)
    return out


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
        reached = row.get("reached") or {}
        if row.get("sections"):
            from .extract import extract_sections
            sections = product_sections(hit[1], row["url"])
            if sections:
                records.extend(extract_sections(
                    row["url"], hit[1], sections, manufacturer=vendor, recipe=recipe,
                    category=reached.get("category") or None,
                ))
                continue
        records.extend(
            extract_page(
                row["url"], hit[1], manufacturer=vendor, recipe=recipe,
                category=reached.get("category") or None,
                name_hint=reached.get("name", ""),
            )
        )

    records = merge_cross_page_duplicates(records)

    # Datasheet specs (pdfs.py) fill gaps on single-device pages. The page wins on any
    # key it already has; every merged value says where it came from.
    from .normalize import attribute_key, parse_spec_value
    from .pdfs import load_datasheet_specs

    datasheets = load_datasheet_specs(out)
    per_url: dict[str, int] = {}
    for r in records:
        per_url[r.url] = per_url.get(r.url, 0) + 1
    for r in records:
        sheet = datasheets.get(r.url)
        if not sheet or not sheet.get("specs") or per_url[r.url] != 1:
            continue
        added = 0
        for attr, raw in sheet["specs"].items():
            key = attribute_key(attr)
            if key and key not in r.specs:
                value = parse_spec_value(raw)
                value.source = f"datasheet:{sheet['pdf']}"
                r.specs[key] = value
                added += 1
        if added:
            r.warnings.append(f"{added} specs read from datasheet {sheet['pdf']}")
            if r.spec_source in ("", "none"):
                r.spec_source = "datasheet"

    # Zero specs means no table on the page passed validation — in practice a series or
    # overview page. Strong indicator, not proof, so these are held for review rather than
    # dropped: a real device documented without a table must stay visible.
    store = VerdictStore.load(out)

    def accepted(record) -> bool:
        decided = store.review_for(record.product_id)
        if decided:
            return decided["verdict"] == "device"
        return is_device_row(record)

    # Automation relevance (judge.screen_relevance): only a clear "no" leaves the
    # table, and it goes to excluded.csv with its reason rather than disappearing.
    def irrelevant(record) -> dict | None:
        v = store.relevance_for(record.product_id)
        return v if v and v["verdict"] == "no" else None

    devices = [r for r in records if accepted(r) and not irrelevant(r)]
    excluded = [r for r in records if accepted(r) and irrelevant(r)]
    review = [
        r for r in records
        if not accepted(r) and store.review_for(r.product_id) is None
    ]

    write_jsonl(records, out / "extracted.jsonl")          # lossless: everything
    write_csv(to_wide(devices), out / "devices.csv", list(CORE_COLUMNS))
    write_csv(to_eav(devices), out / "specs_eav.csv")
    write_csv(to_review_queue(review), out / "review_queue.csv")
    write_csv(
        [{"product_id": r.product_id, "name": r.name, "category": r.category,
          "url": r.url, "reason": irrelevant(r)["reason"]} for r in excluded],
        out / "excluded.csv",
        ["product_id", "name", "category", "url", "reason"],
    )

    with_specs = len(devices)
    merged = sum(1 for r in devices if len(r.source_variants) > 1)
    summary = {
        "domain": domain,
        "manufacturer": vendor,
        "pages_extracted": len({r.url for r in records}),
        "devices": len(devices),
        "held_for_review": len(review),
        "excluded_not_automatable": len(excluded),
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
