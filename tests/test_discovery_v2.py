"""Menu-driven scope, focused crawl, reasoning backend and cost accounting.

Every case here reproduces a failure seen on one of the six vendors the second
generation was built against (Agilent, Azenta, Beckman Coulter, Brooks, Formulatrix,
Tecan). The HTML is synthetic but structurally faithful, so the suite stays offline.
The model is a fake backend: what is tested is the plumbing around judgment — ids
mapped back to URLs, budgets enforced, verdicts persisted, cost counted — not the
judgment itself.
"""

from __future__ import annotations

import json

import pytest

from prodscrape import navigation as nav
from prodscrape.discover import sitemap_priority
from prodscrape.fetch import looks_like_challenge
from prodscrape.harvest import EDITORIAL_PATH, Candidate, Frontier, _key
from prodscrape.judge import classify_pages, triage_links
from prodscrape.llm import Backend, BudgetExceeded, Ledger, Reasoner, Usage, parse_json
from prodscrape.scope import (
    ScopeDecision, ScopeEntry, SiteContext, decide_heuristically, decide_with_model,
)
from prodscrape.signals import PageSignals, classify_by_signals
from prodscrape.verdicts import VerdictStore


class FakeBackend(Backend):
    """Answers from a callable and bills a fixed amount per call."""

    name = "fake"

    def __init__(self, answer, cost=0.01):
        super().__init__("claude-opus-5")
        self.answer, self.cost, self.calls = answer, cost, []

    def complete(self, system, user, *, effort):
        self.calls.append((system, user, effort))
        text = self.answer(user)
        return text, Usage(stage="", backend=self.name, model=self.model,
                           input_tokens=len(user) // 4, output_tokens=20,
                           cost_usd=self.cost)


MEGA_MENU = """
<html><body class="home mega-menu-header">
<header><nav><ul>
  <li><a href="/products">Products</a><ul>
    <li><a href="/products/liquid-handling">Liquid handling</a><ul>
      <li><a href="/fluent">Fluent®</a><ul>
        <li><a href="/fluent?tab=1">Overview</a></li>
        <li><a href="/fluent?tab=2">Software</a></li>
      </ul></li>
    </ul></li>
  </ul></li>
  <li><a href="https://lifesciences.example.com/">Life Sciences</a></li>
  <li><a href="https://www.brooks-lab.com/">Lab Automation</a></li>
  <li><a href="https://x.com/example">X</a></li>
</ul></nav></header>
<main><p>Welcome.</p>
  <div class="card"><a href="/fluent?hsLang=en">Read More about Fluent® Workstation</a></div>
  <a href="/null/doc/brochure-pdf-123">broken</a>
</main>
<footer><a href="/privacy">Privacy</a></footer>
</body></html>
"""


# --- navigation -------------------------------------------------------------------

def test_menu_keeps_hierarchy_and_names_products_not_tabs():
    entries = nav.extract_nav(MEGA_MENU, "https://example.com/", domain="example.com")
    by_path = {e.url.split("example.com")[-1]: e for e in entries if "example.com" in e.url}
    fluent = by_path["/fluent"]
    # Tab links point at the same page; the product's own entry, with its category
    # trail, is the one kept.
    assert fluent.text == "Fluent®"
    assert fluent.trail[-1] == "Liquid handling"


def test_social_filter_matches_domains_not_substrings():
    """"x.com" is a suffix of formulatrix.com; a substring test dropped its whole menu."""
    html = '<nav><a href="https://formulatrix.com/mantis">Mantis</a></nav>'
    entries = nav.extract_nav(html, "https://formulatrix.com/", domain="formulatrix.com")
    assert [e.text for e in entries] == ["Mantis"]
    assert nav.registrable_domain("x.com") in nav.SOCIAL_DOMAINS


def test_external_menu_links_are_kept_only_on_request_and_marked():
    plain = nav.extract_nav(MEGA_MENU, "https://example.com/", domain="example.com")
    assert all("brooks-lab" not in e.url for e in plain)
    wide = nav.extract_nav(MEGA_MENU, "https://example.com/", domain="example.com",
                           include_external=True)
    ext = [e for e in wide if "brooks-lab" in e.url]
    assert ext and ext[0].region == "external"
    assert all("x.com" not in e.url for e in wide)


def test_sibling_hosts_of_the_vendor_are_kept():
    entries = nav.extract_nav(MEGA_MENU, "https://example.com/", domain="example.com")
    assert any(e.url.startswith("https://lifesciences.example.com") for e in entries)


def test_content_links_survive_a_body_whose_class_looks_like_chrome():
    """Azenta's <body class="... mega-menu-header">: the body itself was stripped."""
    links = nav.content_links(MEGA_MENU, "https://example.com/", domain="example.com")
    assert any(l.url.endswith("/fluent") for l in links)


def test_content_links_clean_calls_to_action_and_tracking_and_junk():
    links = nav.content_links(MEGA_MENU, "https://example.com/", domain="example.com")
    fluent = next(l for l in links if l.url.endswith("/fluent"))
    assert fluent.text == "Fluent® Workstation"
    assert "hsLang" not in fluent.url
    assert not any("/null/" in l.url for l in links)


def test_canonical_url_and_junk_detection():
    assert nav.canonical_url("https://a.com/x?hsLang=en&utm_source=y&id=3#top") == \
        "https://a.com/x?id=3"
    assert nav.is_junk_url("https://a.com/null/doc/x")
    assert not nav.is_junk_url("https://a.com/nullarbor-reader")


def test_calls_to_action_do_not_eat_short_names():
    assert nav.clean_link_text("Read More about BioArc Duo") == "BioArc Duo"
    assert nav.clean_link_text("Discover now") == "Discover now"


# --- discovery ----------------------------------------------------------------------

def test_product_sitemaps_are_read_before_ecommerce_and_other_hosts():
    """agilent.com: a 50k-URL e-shop sitemap exhausted the budget before products0.xml."""
    base = "www.agilent.com"
    order = sorted(
        ["https://www.agilent.com/pim_commerce01.xml",
         "https://community.agilent.com/sitemapindex.ashx",
         "https://www.agilent.com/products0.xml",
         "https://www.agilent.com/multimedia0.xml"],
        key=lambda u: sitemap_priority(u, base),
    )
    assert order[0].endswith("products0.xml")
    assert order[-1].startswith("https://community.")


def test_bot_challenge_pages_are_recognised():
    assert looks_like_challenge("<html><head><title>Just a moment...</title>")
    assert looks_like_challenge("<title>Access Denied</title>")
    assert not looks_like_challenge("<title>Microfuge 20 | Beckman</title>")


# --- harvest --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/tecan-journal/topic/fluent", "/scientific-citations", "/doclist-hs/notes",
    "/blog/x", "/events/slas", "/app-notes/y",
])
def test_editorial_paths_are_never_followed(path):
    assert EDITORIAL_PATH.search(path)


@pytest.mark.parametrize("path", ["/multimode-plate-reader", "/products/bioarc-duo",
                                  "/liquid-handlers/echo-acoustic"])
def test_product_paths_are_followed(path):
    assert not EDITORIAL_PATH.search(path)


def test_frontier_skips_menu_entries_the_scope_step_rejected():
    decision = ScopeDecision(hubs=[ScopeEntry(url="https://e.com/readers", name="Readers")])
    menu = [nav.NavEntry(text="Consumables", url="https://e.com/consumables"),
            nav.NavEntry(text="Readers", url="https://e.com/readers")]
    f = Frontier(decision, menu, "e.com", "e.com")
    html = ('<main><a href="/consumables">Consumables</a><a href="/spark">Spark</a>'
            '<a href="/readers?page=2">2</a></main>')
    hub = Candidate(url="https://e.com/readers", name="Readers", via="nav")
    f.expand(html, hub, {"e.com"})
    assert [c.url for c in f.pending] == ["https://e.com/spark"]
    # Pagination is followed as more of the same hub, not as a product.
    assert ("hub", "https://e.com/readers?page=2") in [(k, c.url) for k, c in f.items]
    assert _key("https://e.com/readers?page=2") != _key("https://e.com/readers")


# --- signals --------------------------------------------------------------------------

def test_editorial_heading_does_not_outweigh_product_evidence():
    """Formulatrix product pages carry a "Publications" section and 20 order codes."""
    sig = PageSignals(url="u", depth=2, slug="mantis", headings=["Publications"],
                      order_numbers=["F-1-2"] * 3)
    verdict = classify_by_signals(sig, prior=0.35)
    assert verdict.label == "unknown"          # judged, not dropped


# --- reasoning backend ----------------------------------------------------------------

def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"keep": [1, 2]} done') == {"keep": [1, 2]}


def test_every_model_call_lands_in_the_ledger(tmp_path):
    ledger = Ledger(tmp_path)
    r = Reasoner(FakeBackend(lambda u: '{"keep": [0]}', cost=0.02), ledger, budget_usd=1)
    r.ask_json("triage", "sys", "0 | x", items=5)
    totals = ledger.totals()
    assert totals["model_calls"] == 1
    assert totals["by_stage"]["triage"]["items"] == 5
    assert totals["cost_usd"] == pytest.approx(0.02)


def test_budget_is_a_hard_stop(tmp_path):
    r = Reasoner(FakeBackend(lambda u: "{}", cost=0.6), Ledger(tmp_path), budget_usd=1.0)
    r.ask_json("classify", "s", "u")
    r.ask_json("classify", "s", "u")           # spent 1.2 now
    with pytest.raises(BudgetExceeded):
        r.ask_json("classify", "s", "u")


def test_no_backend_means_agent_path(tmp_path):
    assert not Reasoner(None, Ledger(tmp_path)).available


# --- scope ----------------------------------------------------------------------------

def _ctx():
    entries = [
        nav.NavEntry("Readers", "https://e.com/readers", ["Products"]),
        nav.NavEntry("Spark", "https://e.com/spark", ["Products", "Readers"]),
        nav.NavEntry("Tips", "https://e.com/tips", ["Products", "Consumables"]),
        nav.NavEntry("Careers", "https://e.com/careers", []),
    ]
    return SiteContext(domain="e.com", base_host="e.com", entries=entries)


def test_model_scope_maps_ids_back_to_menu_entries(tmp_path):
    backend = FakeBackend(lambda u: json.dumps(
        {"products": [1], "hubs": [0], "catalogue_prefixes": ["/products/"], "notes": "ok"}))
    decision = decide_with_model(_ctx(), Reasoner(backend, Ledger(tmp_path)))
    assert [e.url for e in decision.products] == ["https://e.com/spark"]
    assert decision.products[0].category == "Readers"
    assert [e.url for e in decision.hubs] == ["https://e.com/readers"]
    assert decision.decided_by == "model"
    assert backend.calls[0][2] == "medium"     # scope is worth more effort than triage


def test_heuristic_scope_without_a_model():
    decision = decide_heuristically(_ctx(), [])
    assert "https://e.com/spark" in [e.url for e in decision.products]
    assert "https://e.com/readers" in [e.url for e in decision.hubs]
    assert "https://e.com/tips" not in [e.url for e in decision.products + decision.hubs]
    assert decision.decided_by == "heuristic"


# --- judgments persist ------------------------------------------------------------------

def test_triage_decisions_are_remembered(tmp_path):
    backend = FakeBackend(lambda u: '{"keep": [0]}')
    r = Reasoner(backend, Ledger(tmp_path))
    cands = [Candidate(url="https://e.com/spark", name="Spark"),
             Candidate(url="https://e.com/citations", name="Citations")]
    known: dict[str, bool] = {}
    assert triage_links(r, cands, known) == [True, False]
    assert triage_links(r, cands, known) == [True, False]
    assert len(backend.calls) == 1             # second pass asked nothing


def test_model_classifications_go_to_the_same_store_as_agent_ones(tmp_path):
    backend = FakeBackend(lambda u: '{"verdicts": [{"i": 0, "label": "consumable", '
                                    '"reason": "sheath fluid"}]}')
    store = VerdictStore.load(tmp_path)
    classify_pages(Reasoner(backend, Ledger(tmp_path)), [{"url": "https://e.com/f"}], store)
    stored = VerdictStore.load(tmp_path).classification_for("https://e.com/f")
    assert stored["label"] == "consumable" and stored["decided_by"] == "model"


# --- the report the agent relays ------------------------------------------------------

def test_tool_results_are_metered_into_the_vendor_ledger(tmp_path, monkeypatch):
    from prodscrape import mcp_server

    monkeypatch.setenv("PRODSCRAPE_HOME", str(tmp_path))
    mcp_server.run_status("e.com")
    rows = Ledger(tmp_path / "runs" / "e.com").rows()
    assert rows and rows[-1]["kind"] == "tool_output" and rows[-1]["tool"] == "run_status"
    assert rows[-1]["tokens"] > 0


def test_report_carries_measured_cost_and_says_what_it_excludes():
    from prodscrape.orchestrate import compose_report

    usage = {"model_calls": 3, "input_tokens": 12000, "output_tokens": 900,
             "cost_usd": 0.123, "by_stage": {"scope": {"calls": 1}, "classify": {"calls": 2}},
             "models": ["claude-opus-5"], "backends": ["claude-cli"],
             "tool_payload_tokens": 0, "tool_calls": 0}
    scan = {"scope": {"decided_by": "model", "products_named": 5, "hubs_named": 2,
                      "hosts": ["e.com"], "affiliated_domains": []},
            "pages_from_archive": 7, "blocked": False, "unvisited_candidates": 0}
    text = compose_report({
        "manufacturer": "E", "devices": 12, "held_for_review": 0,
        "pages_awaiting_judgment": 0, "scan": scan, "usage": usage,
        "run_usage_usd": 0.05, "datasheets": None, "model_review": None,
        "artifacts": {"devices_csv": "devices.csv"},
    })
    assert "12 devices" in text and "$0.123" in text and "12,000 input" in text
    assert "Not included" in text
    assert "Internet Archive" in text


def test_relevance_screen_excludes_only_clear_no(tmp_path):
    from prodscrape.judge import screen_relevance

    backend = FakeBackend(lambda u: '{"verdicts": [{"i": 0, "relevance": "no", "reason": '
                                    '"manual hand tool"}, {"i": 1, "relevance": "maybe", '
                                    '"reason": "benchtop, data port"}]}')
    store = VerdictStore.load(tmp_path)
    result = screen_relevance(Reasoner(backend, Ledger(tmp_path)),
                              [{"product_id": "a", "name": "Manual Picker"},
                               {"product_id": "b", "name": "Microfuge 20"}], store)
    assert result["excluded"] == 1
    assert store.relevance_for("b")["verdict"] == "maybe"


def test_overview_pages_are_excluded_whatever_their_relevance(tmp_path):
    from prodscrape.judge import screen_relevance

    backend = FakeBackend(lambda u: '{"verdicts": [{"i": 0, "single": false, '
                                    '"relevance": "yes", "reason": "category page"}]}')
    store = VerdictStore.load(tmp_path)
    screen_relevance(Reasoner(backend, Ledger(tmp_path)),
                     [{"product_id": "a", "name": "Liquid handling components"}], store)
    v = store.relevance_for("a")
    assert v["verdict"] == "no" and v["reason"].startswith("not a single device")


# --- one table, everywhere; background runs --------------------------------------------

def _write_run(run_dir, table_ids, records):
    import csv as _csv

    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "extracted.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    with (run_dir / "devices.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["product_id", "name"])
        w.writeheader()
        for i in table_ids:
            w.writerow({"product_id": i, "name": i})


def test_views_and_counts_follow_devices_csv_not_the_raw_extraction(tmp_path, monkeypatch):
    """LiCONiC: the browser view showed 26 rows while devices.csv held 32."""
    from prodscrape import mcp_server
    from prodscrape.pipeline import table_records

    monkeypatch.setenv("PRODSCRAPE_HOME", str(tmp_path))
    base = {"manufacturer": "L", "category": "", "url": "u", "description": "",
            "image_url": "", "datasheet_urls": [], "interfaces": [],
            "source_variants": [], "merge_reason": ""}
    records = [
        {**base, "product_id": "accepted-no-specs", "name": "StoreX STX44", "specs": {}},
        {**base, "product_id": "excluded-with-specs", "name": "BiOLiX STC",
         "specs": {"t": {"raw": "37 C"}}},
    ]
    run = tmp_path / "runs" / "l.com"
    _write_run(run, ["accepted-no-specs"], records)
    assert [r["product_id"] for r in table_records(run)] == ["accepted-no-specs"]
    view = mcp_server.open_device_table("l.com", open_browser=False)
    assert view["devices"] == 1
    assert mcp_server.run_status("l.com")["devices"] == 1


def test_background_job_states(tmp_path, monkeypatch):
    import time as _time

    from prodscrape import jobs

    monkeypatch.setenv("PRODSCRAPE_HOME", str(tmp_path))
    run = tmp_path / "runs" / "v.com"
    run.mkdir(parents=True)
    assert jobs.status("v.com")["state"] == "idle"
    log = run / "job.log"
    log.write_text("crawling\n", encoding="utf-8")
    job = {"pid": 1, "started_at": _time.time(), "log": str(log)}
    (run / "job.json").write_text(json.dumps(job), encoding="utf-8")
    jobs.Progress(run)("crawl", "10 pages fetched")
    assert jobs.status("v.com")["state"] == "running"
    log.write_text("Traceback (most recent call last):\n  boom\n", encoding="utf-8")
    assert jobs.status("v.com")["state"] == "failed"
    (run / "catalogue_manifest.json").write_text("{}", encoding="utf-8")
    assert jobs.status("v.com")["state"] == "done"
