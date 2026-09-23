"""Token and cost accounting.

Two numbers matter and they are not the same:

**Estimated before a run** — how many tokens the pipeline expects to hand to the model,
derived from the candidate count and the measured tier-B escalation rate.

**Actually handed over** — the exact size of every digest and queue this run passed to the
agent, tallied as it happens.

What this module *cannot* see: the agent's own context — the conversation, its reasoning,
its tool-call overhead — which is billed too. Treat these figures as the floor of what a
run costs, not the whole bill. The honest way to read them is "the pipeline contributed at
least this much"; the API response's own `usage` field is the only exact source.

Prices are Anthropic first-party rates per million tokens (table cached 2026-06-24).
"""

from __future__ import annotations

from dataclasses import dataclass

# model id -> (input $/MTok, output $/MTok)
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5-1": (10.00, 50.00),
}
DEFAULT_MODEL = "claude-opus-5"

# Measured on the three reference vendors: a page digest is ~180 tokens, and a verdict
# (label plus a one-line reason) is ~40 output tokens.
TOKENS_PER_DIGEST = 180
TOKENS_PER_VERDICT = 40
TOKENS_PER_REVIEW_ROW = 60

# Share of candidates the deterministic signals cannot decide. Observed 20-40% across
# analytik-jena, BINDER and QInstruments; the midpoint is the planning number.
DEFAULT_ESCALATION_RATE = 0.30


def estimate_tokens(text: str) -> int:
    """Rough token count for a JSON-ish payload.

    Deliberately crude — ~3.5 characters per token, which is closer for JSON (heavy in
    punctuation and short keys) than the usual 4. For an exact count, call the API's
    `count_tokens` endpoint; this is for budgeting, not billing.
    """
    return max(1, round(len(text) / 3.5))


@dataclass
class CostEstimate:
    model: str
    input_tokens: int
    output_tokens: int
    input_cost: float
    output_cost: float
    basis: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_cost(self) -> float:
        return self.input_cost + self.output_cost

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_cost_usd": round(self.input_cost, 4),
            "output_cost_usd": round(self.output_cost, 4),
            "total_cost_usd": round(self.total_cost, 4),
            "basis": self.basis,
            "excludes": "the agent's own conversation context, which is billed separately",
        }

    def summary(self) -> str:
        return (
            f"~{self.total_tokens:,} tokens (~${self.total_cost:.3f}) on {self.model} — "
            f"{self.basis}. Excludes the agent's own context."
        )


def price(model: str, input_tokens: int, output_tokens: int) -> CostEstimate:
    if model not in PRICING:
        raise ValueError(f"unknown model {model!r}; known: {sorted(PRICING)}")
    in_rate, out_rate = PRICING[model]
    return CostEstimate(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost=input_tokens / 1_000_000 * in_rate,
        output_cost=output_tokens / 1_000_000 * out_rate,
        basis="measured",
    )


def estimate_scrape(
    candidates: int,
    *,
    model: str = DEFAULT_MODEL,
    escalation_rate: float = DEFAULT_ESCALATION_RATE,
    review_rows: int = 0,
) -> CostEstimate:
    """What a full run over ``candidates`` pages is expected to cost in model calls.

    Only the escalated minority reaches the model; the deterministic tiers are free.
    """
    escalated = round(candidates * escalation_rate)
    input_tokens = escalated * TOKENS_PER_DIGEST + review_rows * TOKENS_PER_REVIEW_ROW
    output_tokens = (escalated + review_rows) * TOKENS_PER_VERDICT
    est = price(model, input_tokens, output_tokens)
    est.basis = (
        f"{escalated} of {candidates} pages escalated at {escalation_rate:.0%}"
        + (f", plus {review_rows} review rows" if review_rows else "")
    )
    return est


def compare_naive(candidates: int, avg_page_bytes: int = 200_000,
                  model: str = DEFAULT_MODEL) -> CostEstimate:
    """What it would cost to send whole pages to the model — the thing we avoid.

    Useful as a sanity check on the architecture: if this number is not dramatically
    larger than ``estimate_scrape``, the token economy is not earning its complexity.
    """
    input_tokens = candidates * estimate_tokens("x" * avg_page_bytes)
    est = price(model, input_tokens, candidates * TOKENS_PER_VERDICT)
    est.basis = f"all {candidates} pages sent in full (~{avg_page_bytes // 1000}KB each)"
    return est


def calibrated_estimate(runs_root, *, exclude: str | None = None) -> dict | None:
    """What a new vendor is likely to cost, from what previous vendors *measurably* cost.

    The old estimate multiplied a fixed escalation rate by a fixed digest size and was
    wrong in both directions. This one reads every ``runs/*/ledger.jsonl`` on disk and
    reports the median and range of real per-vendor spend. With no history it returns
    ``None`` — saying "no basis for an estimate" beats inventing one.
    """
    import json
    import statistics
    from pathlib import Path

    costs: list[float] = []
    tokens: list[int] = []
    vendors: list[str] = []
    for ledger in Path(runs_root).glob("*/ledger.jsonl"):
        if exclude and ledger.parent.name == exclude:
            continue
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        model = [r for r in rows if r.get("kind", "model") == "model"]
        if not model:
            continue
        vendors.append(ledger.parent.name)
        costs.append(sum(r.get("cost_usd", 0.0) for r in model))
        tokens.append(sum(r.get("input_tokens", 0) + r.get("cache_read_tokens", 0)
                          + r.get("cache_write_tokens", 0) + r.get("output_tokens", 0)
                          for r in model))
    if not costs:
        return None
    return {
        "basis": f"measured model spend of {len(costs)} previous vendor runs",
        "vendors": sorted(vendors),
        "median_usd": round(statistics.median(costs), 3),
        "min_usd": round(min(costs), 3),
        "max_usd": round(max(costs), 3),
        "median_tokens": int(statistics.median(tokens)),
    }
