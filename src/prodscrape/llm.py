"""The reasoning backend — where the pipeline asks a model, and what it paid.

The deterministic tiers decide most things for free. A few decisions are genuinely
semantic, and hardcoding them was the tool's weakest point across vendors:

* **scope** — which part of a site is the instrument catalogue. One call per vendor,
  over the navigation menu and a URL tree (~3-6k tokens). Replaces a keyword heuristic
  that picked Azenta's blog as its catalogue.
* **classify** — instrument vs accessory vs application page, for the pages structural
  signals cannot settle. Batched digests, ~200 tokens per page.
* **review** — records that produced no specs: a real device or an overview page?

Backends, first available wins:

1. ``anthropic`` — the Anthropic SDK, when credentials resolve (``ANTHROPIC_API_KEY`` or
   an ``ant auth login`` profile). Usage is read from each response.
2. ``claude-cli`` — headless ``claude -p`` using the user's Claude login. Called with its
   own system prompt, no tools and no MCP servers, so a call carries ~400 tokens of
   overhead rather than the ~25k a default Claude Code session starts with. Reports
   exact usage and cost.
3. ``agent`` — no backend. The MCP tools hand the same questions to the driving agent
   instead (``pending_*`` / ``record_*``), and nothing is billed here.

Every call is appended to ``runs/<domain>/ledger.jsonl`` with its measured tokens and
cost, so a run's model spend is *counted*, never estimated after the fact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from .costs import price

ENV_BACKEND = "PRODSCRAPE_LLM"          # anthropic | claude-cli | agent | auto
ENV_MODEL = "PRODSCRAPE_MODEL"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_BUDGET_USD = 1.00               # per vendor run; a hard stop, not a hint


class BudgetExceeded(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class Usage:
    stage: str
    backend: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    items: int = 0                      # how many decisions this call made
    at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class Ledger:
    """Append-only record of every model call and every tool payload of a run."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "ledger.jsonl"

    def append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    def model_rows(self) -> list[dict]:
        return [r for r in self.rows() if r.get("kind", "model") == "model"]

    def spent_usd(self) -> float:
        return sum(r.get("cost_usd", 0.0) for r in self.model_rows())

    def totals(self) -> dict:
        """Model spend by stage, plus the tool payloads handed to a driving agent."""
        by_stage: dict[str, dict] = {}
        for r in self.model_rows():
            s = by_stage.setdefault(r["stage"], {
                "calls": 0, "items": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
            })
            s["calls"] += 1
            s["items"] += r.get("items", 0)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens"):
                s[k] += r.get(k, 0)
            s["cost_usd"] = round(s["cost_usd"] + r.get("cost_usd", 0.0), 6)
        tool_rows = [r for r in self.rows() if r.get("kind") == "tool_output"]
        backends = sorted({r["backend"] for r in self.model_rows()})
        models = sorted({r["model"] for r in self.model_rows()})
        return {
            "model_calls": sum(s["calls"] for s in by_stage.values()),
            "input_tokens": sum(s["input_tokens"] + s["cache_read_tokens"]
                                + s["cache_write_tokens"] for s in by_stage.values()),
            "output_tokens": sum(s["output_tokens"] for s in by_stage.values()),
            "cost_usd": round(sum(s["cost_usd"] for s in by_stage.values()), 4),
            "by_stage": by_stage,
            "backends": backends,
            "models": models,
            "tool_payload_tokens": sum(r.get("tokens", 0) for r in tool_rows),
            "tool_calls": len(tool_rows),
        }


# --------------------------------------------------------------------------- parsing

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def parse_json(text: str):
    """The JSON object in a model reply, tolerating fences and surrounding prose."""
    text = _FENCE_RE.sub("", text.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"model reply is not JSON: {text[:200]!r}")


# --------------------------------------------------------------------------- backends

class Backend:
    name = "none"

    def __init__(self, model: str):
        self.model = model

    def complete(self, system: str, user: str, *, effort: str) -> tuple[str, Usage]:
        raise NotImplementedError


class AnthropicBackend(Backend):
    name = "anthropic"

    def __init__(self, model: str):
        super().__init__(model)
        import anthropic                       # optional dependency: prodscrape[api]
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()

    def complete(self, system: str, user: str, *, effort: str) -> tuple[str, Usage]:
        started = time.monotonic()
        response = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined the request")
        text = "".join(b.text for b in response.content if b.type == "text")
        u = response.usage
        cost = price(self.model, u.input_tokens, u.output_tokens).total_cost
        in_rate = price(self.model, 1_000_000, 0).total_cost
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        cost += cache_read / 1e6 * in_rate * 0.1 + cache_write / 1e6 * in_rate * 1.25
        return text, Usage(
            stage="", backend=self.name, model=self.model,
            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            cost_usd=round(cost, 6), seconds=round(time.monotonic() - started, 2),
        )


class ClaudeCLIBackend(Backend):
    """``claude -p`` with a replacement system prompt and every tool switched off."""

    name = "claude-cli"

    def __init__(self, model: str, executable: str):
        super().__init__(model)
        self.executable = executable

    def complete(self, system: str, user: str, *, effort: str) -> tuple[str, Usage]:
        started = time.monotonic()
        cmd = [
            self.executable, "-p",
            "--model", self.model,
            # Effort drives thinking, and thinking is billed as output: a triage batch
            # cost ~3k output tokens at the default effort for a reply of ~500.
            "--effort", effort,
            "--output-format", "json",
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, encoding="utf-8",
            timeout=600,
            # Run outside any project so no CLAUDE.md or project settings are loaded.
            cwd=os.path.expanduser("~"),
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"claude CLI failed: {proc.stderr.strip()[:300]}")
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI error: {str(data.get('result'))[:300]}")
        u = data.get("usage", {})
        return str(data.get("result", "")), Usage(
            stage="", backend=self.name, model=self.model,
            input_tokens=int(u.get("input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
            cache_write_tokens=int(u.get("cache_creation_input_tokens", 0)),
            cost_usd=round(float(data.get("total_cost_usd", 0.0)), 6),
            seconds=round(time.monotonic() - started, 2),
        )


def _anthropic_credentials_present() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def resolve_backend(preference: str | None = None, model: str | None = None) -> Backend | None:
    """The first usable backend, or ``None`` when judgment must go to the agent."""
    preference = (preference or os.environ.get(ENV_BACKEND) or "auto").lower()
    model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
    if preference == "agent":
        return None

    if preference in ("auto", "anthropic") and _anthropic_credentials_present():
        try:
            return AnthropicBackend(model)
        except ImportError:
            if preference == "anthropic":
                raise LLMUnavailable("install the api extra: prodscrape[api]")

    if preference in ("auto", "claude-cli"):
        exe = shutil.which("claude")
        if exe:
            return ClaudeCLIBackend(model, exe)
        if preference == "claude-cli":
            raise LLMUnavailable("the `claude` CLI is not on PATH")
    return None


class Reasoner:
    """Budgeted, ledgered access to a backend for one vendor run."""

    def __init__(self, backend: Backend | None, ledger: Ledger,
                 budget_usd: float = DEFAULT_BUDGET_USD):
        self.backend = backend
        self.ledger = ledger
        self.budget_usd = budget_usd

    @property
    def available(self) -> bool:
        return self.backend is not None

    def remaining_usd(self) -> float:
        return self.budget_usd - self.ledger.spent_usd()

    def ask_json(self, stage: str, system: str, user: str, *, items: int = 1,
                 effort: str = "low", retries: int = 1):
        """One question, parsed JSON back, usage appended to the ledger."""
        if self.backend is None:
            raise LLMUnavailable("no reasoning backend; hand this to the agent")
        if self.remaining_usd() <= 0:
            raise BudgetExceeded(
                f"model budget of ${self.budget_usd:.2f} for this run is spent"
            )
        last_error: Exception | None = None
        for _ in range(retries + 1):
            text, usage = self.backend.complete(system, user, effort=effort)
            usage.stage, usage.items = stage, items
            usage.at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.ledger.append({"kind": "model", **usage.as_dict()})
            try:
                return parse_json(text)
            except ValueError as exc:
                last_error = exc
                user = user + "\n\nReply with the JSON object only."
        raise ValueError(str(last_error))
