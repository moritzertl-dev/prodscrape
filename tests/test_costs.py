"""Token and cost accounting."""

from __future__ import annotations

import pytest

from prodscrape.costs import (
    DEFAULT_MODEL, PRICING, compare_naive, estimate_scrape, estimate_tokens, price,
)
from prodscrape.verdicts import VerdictStore


def test_default_model_is_priced():
    assert DEFAULT_MODEL in PRICING
    assert PRICING[DEFAULT_MODEL] == (5.00, 25.00)   # Claude Opus 5, $/MTok


def test_price_arithmetic():
    est = price("claude-opus-5", 1_000_000, 1_000_000)
    assert est.input_cost == pytest.approx(5.00)
    assert est.output_cost == pytest.approx(25.00)
    assert est.total_cost == pytest.approx(30.00)


def test_unknown_model_is_rejected():
    with pytest.raises(ValueError, match="unknown model"):
        price("gpt-9", 100, 100)


def test_estimate_scales_with_escalation_rate():
    low = estimate_scrape(100, escalation_rate=0.1)
    high = estimate_scrape(100, escalation_rate=0.5)
    assert high.total_tokens > low.total_tokens
    assert "10 of 100" in low.basis


def test_digest_approach_is_orders_of_magnitude_cheaper():
    """If this stops being true, the token economy is not earning its complexity."""
    smart = estimate_scrape(56, review_rows=24)
    naive = compare_naive(56)
    assert naive.total_cost / smart.total_cost > 100


def test_estimate_states_what_it_excludes():
    est = estimate_scrape(50)
    assert "agent" in est.as_dict()["excludes"]
    assert "Excludes the agent's own context" in est.summary()


def test_token_estimate_is_monotonic():
    assert estimate_tokens("x" * 350) == 100
    assert estimate_tokens("a" * 700) > estimate_tokens("a" * 350)


def test_usage_accumulates_and_persists(tmp_path):
    store = VerdictStore.load(tmp_path)
    assert store.usage == {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    store.add_usage(1200, 300)
    store.add_usage(800, 200)
    store.save()

    reloaded = VerdictStore.load(tmp_path)
    assert reloaded.usage == {"input_tokens": 2000, "output_tokens": 500, "calls": 2}
    actual = price(DEFAULT_MODEL, **{k: v for k, v in reloaded.usage.items() if k != "calls"})
    assert actual.total_cost > 0
