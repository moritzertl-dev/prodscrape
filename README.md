# prodscrape

Turn a manufacturer's website into a reviewed table of their devices.

Point it at a domain; get back one row per device, with specifications, connectivity and a
link to the page every value came from. It runs as an agent driving deterministic Python
tools over MCP — the code does the fetching, parsing and normalising, and the agent is
asked only for the judgments that structure cannot settle.

Built for lab-instrument vendors, but nothing in it is specific to that industry: it
assumes only that a vendor publishes product pages with specification tables.

## What you get

| file | contents |
|---|---|
| `devices.csv` | one row per device — core columns plus a `specs` JSON bag |
| `devices.html` | a sortable, searchable view of the same data, opened in your browser |
| `specs_eav.csv` | the lossless master: one row per (device, attribute), every value with its raw source text |
| `review_queue.csv` | rows that need a human or agent verdict, never silently dropped |
| `excluded.csv` | devices judged unusable in an automated lab (manual tools, passive parts), with the reason |
| `extracted.jsonl` | everything, including provenance and merge reasoning |

## Install

Nothing to clone. The only prerequisite is [`uv`](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/moritzertl-dev/prodscrape prodscrape-mcp
```

Add it to Claude Desktop or Claude Code and ask it to catalogue a vendor. Full setup,
including the config block and troubleshooting: [`SETUP.md`](SETUP.md).

From a checkout:

```bash
uv sync && uv run pytest -q     # 177 tests, fully offline
uv run prodscrape run <domain> --manufacturer "Name"
uv run prodscrape tree <domain>
```

## How it works

One call — `catalogue_vendor(domain)` over MCP, or `prodscrape run <domain>` — runs every
stage and returns a report whose cost figures are measured, not estimated.

**Discover.** `robots.txt`, declared sitemaps (product sitemaps first, e-shop and media
sitemaps last), probes of the usual paths, a bounded crawl when none of that works. When
a vendor blocks automated access outright (Akamai, Cloudflare challenges), the newest
successful public Internet Archive capture of each page is used instead, and every such
record and the final report say so. Bot protection is never circumvented.

**Scope — where do the instruments live?** Read from the vendor's own navigation menu
with its hierarchy (`Products > Microplate readers > Spark®`), sibling hosts
(`lifesciences.tecan.com`) and off-domain product sites (Brooks → PreciseFlex), plus a
count-summarised URL tree. One model call turns that ~3-6k-token digest into product
pages, category hubs and catalogue branches. Without a model, a keyword heuristic
decides and the report says so. The decision is saved in the recipe and replayed.

**Focused crawl.** Category pages are followed to the products they list. Links are
**triaged by the model before any fetch** — ~15 tokens per link instead of a page fetch
and a digest — and large sitemap branches are triaged as branches first. Editorial
paths, tracking parameters, template junk and redirected duplicates are removed
deterministically.

**Classify.** Structural signals decide the clear-cut majority for free; the ambiguous
rest is judged from ~250-token digests, batched.

**Extract.** Spec tables (orientation detected, wrong tables rejected), `<dl>` lists,
specs written as `Label: value` text under a spec heading or accordion, several products
presented as sections of one page, and datasheet PDFs for thin pages. Every value keeps
its raw text and, if it did not come from the page, its source.

**Decide what is a device.** One row per device: non-functional variants merge, the same
device on two pages merges, a generic shared title never does.

## Where reasoning is used — and where it is not

| decision | how | why |
|---|---|---|
| which part of the site is the catalogue | model, once per vendor, cached in the recipe | semantic; keyword rules were right on 1 of 6 new vendors |
| which harvested links / sitemap branches are worth fetching | model, batched, cached | a link text says "citation" or "Spark®" at ~15 tokens; a fetch costs seconds |
| what an ambiguous page is | model over a compact digest | instrument vs accessory vs application is judgment |
| is a spec-less record a device | model over a one-line row | same |
| fetching, parsing tables and text, units, device identity, export | deterministic code | a wrong table produces confident nonsense; code is auditable and free |

**On a Claude subscription (Pro/Max, no API key)** the `claude` CLI backend uses your
Claude Code login: the calls count against your plan's usage limits and are not billed
per token. The report's "$" figure is then the CLI's list-price equivalent — a measure of
size, not a bill. Without the CLI, the chat agent (Claude Desktop) does the judging
itself, which also runs on your plan.

**Speed.** Pages are fetched 6 at a time with at least 0.25 s between requests to the
same host (a robots.txt `Crawl-delay` wins if larger; the Internet Archive gets 1.5 s).
Model batches run 3 at a time. Most of a run's time is the vendor's own response time,
which is why link triage — fetching fewer pages — matters more than the delay.

Judgments come from the first available backend: the Anthropic API (credentials in the
environment), else the local `claude` CLI (your Claude login, no tools, ~400 tokens of
overhead per call), else the driving agent through `pending_*` / `record_*` tools. All
three write the same verdict store, and every call lands in `runs/<domain>/ledger.jsonl`
with its measured tokens and cost. A per-run budget (`--budget`, default $1) is a hard
stop.

## Two principles it is built on

**Python never guesses; the model never parses HTML.** Deterministic code owns
fetching, parsing, identity and export. The model sees menus, link lists and digests —
never a page.

**Judgments are artifacts.** Scope decisions, triage decisions and verdicts are stored
and replayed, so a question is never asked twice and a re-run is free.

## Cost

Measured on the six second-generation vendors with `claude-opus-5`: roughly
**$0.15-0.35 per vendor** for small and mid-size catalogues, up to the budget cap for
very large ones (Agilent). `cost_report(domain)` returns the ledger; `estimate_cost`
quotes the median and range of previous vendors' measured spend, or says there is no
basis yet.

## Per-vendor recipes

The first run against a new vendor infers its rules from URL structure and reports what it
guessed, including where it is unsure. Once those rules are confirmed they are saved as a
recipe, and every later run is fully deterministic — no exploration, no model calls.

Recipes are data, not code. A recipe you write in your own data directory overrides one
shipped with the package, so an install can be corrected without touching it. A few are
bundled as worked examples; they are never required, and the tool runs on vendors it has
never seen.

## Honest limits

- **Bot-protected sites are read from the Internet Archive.** Some vendors return 403
  to anything that isn't a real browser session. The tool never circumvents that; it
  uses public archive captures (possibly weeks old) and says so in the report.
- **No JavaScript rendering.** Static HTML only. Pages that build their content in the
  browser come back empty, and the tool says so.
- **PDF extraction is conservative.** Ruled tables and `Label: value` lines are read;
  free-layout brochures and form-gated downloads are not.
- **Prose specifications stay as text.** A value written as a sentence is kept verbatim
  rather than half-parsed into a number. A low structured-parse rate means the vendor
  writes prose, not that extraction failed — the data is all there.
- **Units are captured, not converted.** Cross-vendor comparison on a numeric spec still
  needs a normalisation pass.

## Documentation

- [`SETUP.md`](SETUP.md) — installing it in Claude Desktop or Claude Code, and sharing it
- [`NOTES.md`](NOTES.md) — field notes: real vendor quirks and how each is handled
- [`PIPELINE.md`](PIPELINE.md) — the design spec: stages, schema, reproducibility contract
- [`src/prodscrape/SKILL.md`](src/prodscrape/SKILL.md) — the procedure the agent follows.
  It ships inside the package and is served by the `get_procedure` tool, so it always
  matches the installed version.
