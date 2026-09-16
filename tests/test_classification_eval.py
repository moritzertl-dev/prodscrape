"""Stage 2 evaluation - the number that decides whether the pipeline is trustworthy.

Runs tier-A deterministic signals over the labelled golden set and asserts precision and
model-call rate. Ambiguous boundary cases (autosampler, sample introduction) are excluded
from the precision assertion until the instrument/accessory boundary is ruled on, but are
still reported so the decision is visible.
"""

from __future__ import annotations

import pytest

from prodscrape.inventory import url_depth
from prodscrape.signals import classify_by_signals, page_signals

FAMILY_DEPTH = 5
SLUG_SUFFIX = "-series"


def _verdicts(cache, golden_labels):
    out = []
    for item in golden_labels:
        hit = cache.get(item["url"])
        assert hit is not None, f"golden cache missing {item['url']}"
        sig = page_signals(item["url"], hit[1])
        verdict = classify_by_signals(
            sig, family_depth=FAMILY_DEPTH, slug_suffix=SLUG_SUFFIX
        )
        out.append((item, sig, verdict))
    return out


def test_golden_set_covers_the_trap_cases(golden_labels):
    labels = {i["label"] for i in golden_labels}
    assert {"instrument", "category_page", "other"} <= labels
    assert any(i.get("ambiguous") for i in golden_labels), "keep boundary cases in the set"


def test_precision_on_instruments(cache, golden_labels):
    """No non-instrument may be labelled 'instrument'. Precision is the metric that
    matters: a false positive pollutes the final table, a false negative just needs a
    second look."""
    results = _verdicts(cache, golden_labels)
    false_positives = [
        (i["url"], v.reason)
        for i, _, v in results
        if v.label == "instrument" and i["label"] != "instrument"
    ]
    assert not false_positives, f"false positives: {false_positives}"


def test_clear_instruments_are_caught_without_a_model_call(cache, golden_labels):
    """Unambiguous instruments must resolve in tier A - that is the token budget."""
    results = _verdicts(cache, golden_labels)
    clear = [
        (i, v)
        for i, _, v in results
        if i["label"] == "instrument" and not i.get("ambiguous")
    ]
    missed = [(i["url"], v.label, v.confidence, v.reason) for i, v in clear if v.label != "instrument"]
    assert not missed, f"clear instruments not caught by signals: {missed}"


def test_model_call_rate_is_low(cache, golden_labels):
    """Fraction escalated to tier B. Each 'unknown' costs tokens."""
    results = _verdicts(cache, golden_labels)
    unknown = [i["url"] for i, _, v in results if v.label == "unknown"]
    rate = len(unknown) / len(results)
    print(f"\ntier-B escalation rate: {rate:.0%} ({len(unknown)}/{len(results)})")
    assert rate <= 0.25, f"too many pages need a model call: {unknown}"


def test_press_release_is_not_an_instrument(cache, golden_labels):
    """A press release names a product and links PDFs - the classic false-positive trap."""
    item = next(i for i in golden_labels if "press-releases/slas-2018" in i["url"])
    hit = cache.get(item["url"])
    sig = page_signals(item["url"], hit[1])
    verdict = classify_by_signals(sig, family_depth=FAMILY_DEPTH, slug_suffix=SLUG_SUFFIX)
    assert verdict.label != "instrument"


def test_category_pages_rejected(cache, golden_labels):
    results = _verdicts(cache, golden_labels)
    bad = [
        (i["url"], v.confidence)
        for i, _, v in results
        if i["label"] == "category_page" and v.label == "instrument"
    ]
    assert not bad, f"taxonomy pages misread as instruments: {bad}"


def test_jsonld_is_absent_on_this_vendor(cache, golden_labels):
    """Documents the finding that forced signal-based classification (PIPELINE.md §6)."""
    results = _verdicts(cache, golden_labels)
    assert not any(s.has_jsonld_product for _, s, _ in results)


def test_structural_prefilter_shrinks_the_candidate_set(site_urls):
    """Depth + slug rules must cut 854 URLs to a reviewable shortlist for free."""
    from prodscrape.inventory import select_candidates

    candidates = select_candidates(
        site_urls,
        include=["/products/*"],
        exclude=["/knowledge/*", "/company/*"],
        depth=FAMILY_DEPTH,
    )
    assert 30 <= len(candidates) <= 80
    assert len(candidates) < len(site_urls) * 0.1


def test_widget_headings_do_not_trigger_the_editorial_penalty(cache, golden_labels):
    """A product page with a "Publication Finder" widget must not be penalised as
    editorial content. Substring matching on "publication" used to cost it 0.5."""
    item = next(i for i in golden_labels if "plasmaquant-ms-series" in i["url"])
    hit = cache.get(item["url"])
    sig = page_signals(item["url"], hit[1])
    assert any("Publication Finder" in h for h in sig.headings), "fixture lost the widget"
    verdict = classify_by_signals(sig, family_depth=FAMILY_DEPTH, slug_suffix=SLUG_SUFFIX)
    assert "editorial" not in verdict.reason
    assert verdict.confidence >= 0.9


def test_weak_positive_evidence_escalates_rather_than_drops():
    """A candidate with thin but non-negative evidence must reach the model, not vanish.

    Regression: qinstruments.com's BioShake Q2 / Q1 3.0 mm / D30-T are real products on
    thin pages whose only signal was URL depth. Scoring 0.15 they were labelled 'other'
    and silently discarded."""
    from prodscrape.signals import PageSignals

    thin = PageSignals(url="https://x.test/automation/bioshake-q2", depth=2, slug="bioshake-q2")
    verdict = classify_by_signals(thin, family_depth=2)
    assert verdict.label == "unknown", "thin candidate must escalate, not be dropped"


def test_pages_with_no_signals_at_all_are_still_dropped():
    """The escalation rule must not turn every page into a model call."""
    from prodscrape.signals import PageSignals

    nothing = PageSignals(url="https://x.test/a/b", depth=9, slug="b")
    assert classify_by_signals(nothing, family_depth=2).label == "other"


def test_negative_evidence_still_drops(cache, golden_labels):
    """A press release must be dropped outright, not escalated."""
    item = next(i for i in golden_labels if "press-releases/slas-2018" in i["url"])
    sig = page_signals(item["url"], cache.get(item["url"])[1])
    assert classify_by_signals(sig, family_depth=FAMILY_DEPTH).label == "other"
