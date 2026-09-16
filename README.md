# prodscrape

Reproducible, token-frugal product-catalogue scraper for lab instrument vendors.
Design spec: [`PIPELINE.md`](PIPELINE.md).

All stages are implemented: discover → shortlist → classify → extract → export, plus an
MCP server (`prodscrape-mcp`) and [`SKILL.md`](SKILL.md) so an agent can drive it.

## Install

```bash
uv sync
uv run pytest -q          # 96 tests, fully offline (they read the golden HTTP cache)
```

## Running it on a new vendor

### 1. Reconnaissance — 4 HTTP requests, no page fetches

```bash
uv run prodscrape tree binder-world.com
```

Prints the platform, whether a sitemap exists, the path-prefix tree, and — if no recipe
exists yet — the rules it would infer. Read the tree and sanity-check the guess.

### 2. Write a recipe (optional but recommended)

If the inferred `include` / `family_depth` are right, skip this. Otherwise copy
`recipes/analytik-jena.com.yaml` to `recipes/<domain>.yaml` and correct it. The most
common corrections are:

- **locale** — a multilingual site's biggest language branch is not necessarily the one
  you want. BINDER's inference picked `de-de` because it has the most URLs; the recipe
  pins `int-en`.
- **family_depth** — the path depth holding product pages.
- **spec_headings / order_headings** — vendor wording. These *extend* the built-in
  defaults, so a partial recipe can never classify worse than no recipe.

A saved recipe is what makes later runs free: it removes the exploration that would
otherwise cost model calls.

### 3. Scan

```bash
uv run prodscrape scan binder-world.com --limit 30   # --limit 0 for no cap
uv run prodscrape show binder-world.com              # re-print the summary
```

Be polite: `--delay` defaults to 1.0s between requests to a host.

### 4. Extract the table

```bash
uv run prodscrape extract binder-world.com --manufacturer "BINDER"
```

Runs **fully offline** off the HTTP cache — every page it needs was fetched during the
scan. Produces `devices.csv` (one row per device), `specs_eav.csv` (the lossless master),
`review_queue.csv` (rows needing a judgment call) and `extracted.jsonl`.

## What Stage 2 produces

Artifacts land in `runs/<domain>/`:

| file | contents |
|---|---|
| `urls_raw.jsonl` | every URL the sitemaps advertise, with `lastmod` |
| `tree.txt` | the path-prefix tree the agent reads instead of raw URLs |
| `candidates.jsonl` | the shortlist after deterministic include/exclude/depth filtering |
| `classified.jsonl` | **one verdict per candidate** — the Stage 2 output |
| `summary.md` | human-reviewable table, grouped by label, sorted by confidence |
| `manifest.json` | config, counts, timings, recipe used — the reproducibility record |
| `extracted.jsonl` | Stage 3 device records, with specs, provenance and merge reasons |
| `devices.csv` | **one row per device**: core columns + a `specs` JSON bag. Devices only — no scrape metadata |
| `review_queue.csv` | records with no spec table: series/overview pages, held for a verdict |
| `specs_eav.csv` | the lossless master: one row per (device, attribute) |

One `classified.jsonl` record:

```jsonc
{
  "url": "https://www.analytik-jena.com/products/.../plasmaquant-ms-series/",
  "label": "instrument",          // instrument | unknown | other | error
  "confidence": 1.0,
  "reason": "has a technical-data heading; has an order-information heading; 20 order numbers present; ...",
  "decided_by": "signals",        // signals | model | recipe | fetch
  "signals": {                    // the raw evidence behind the verdict
    "depth": 5, "slug": "plasmaquant-ms-series",
    "h1": "PlasmaQuant MS Series ICP-MS Tailored to Your Requirements",
    "headings": ["..."], "table_count": 11,
    "has_jsonld_product": false, "has_spec_heading": true, "has_order_heading": true,
    "order_numbers": ["418-88002-0", "..."], "pdf_links": 122, "text_length": 18342
  }
}
```

`label: "unknown"` means the structural evidence was inconclusive — **these, and only
these, are what Stage 2 tier B sends to the model.** The escalation rate printed in
`summary.md` is therefore the per-run token cost in a single number.

## Caching and reproducibility

Every response is stored content-addressed under `cache/<domain>/`. A re-run reads the
cache and never silently re-fetches:

```
analytik-jena.com, 56 pages:  first run 401s  →  cached re-run 5.9s
```

Tests run entirely off `tests/golden/cache`, so they are offline and deterministic.

## Current results

| vendor | platform | discovery | URLs | candidates | instrument | unknown | escalation |
|---|---|---|---|---|---|---|---|
| analytik-jena.com | TYPO3 | sitemap (probed) | 854 | 56 | 44 | 12 | 21% |
| binder-world.com | — | sitemap (robots) | 2,519 | 99 | 87 | 12 | 12% |
| qinstruments.com | TYPO3 | **BFS crawl** | 127 | 20 | 13 | 7 | 35% |

The escalation buckets differ in kind, which matters more than the rate:

- **analytik-jena** — the unknowns are genuinely ambiguous (disposable tips, pipetting
  heads, workflow/application pages). Exactly the pages worth spending tokens on.
- **binder-world** — all 12 unknowns are real instruments on thin variant pages carrying
  no spec table at all (`KB PRO 260 with ICH light module`). Not ambiguity; these are
  configuration variants of a parent model and need family roll-up plus spec inheritance.

Discovery failure modes seen so far, all three of them different:

- **analytik-jena** — robots.txt declares no sitemap, but `/sitemap.xml` exists. Probing
  default paths is what finds it.
- **binder-world** — 49 sub-sitemaps declared in robots, heavily multilingual.
- **qinstruments** — robots.txt declares `/sitemap.xml`, which returns HTTP **200** with
  the body `Invalid error handler configuration: t3://page?uid=234`. The vendor's sitemap
  is broken, so a 200 status is validated before it is trusted, and discovery falls back
  to a bounded BFS crawl. The bare domain also has no DNS record — only `www` resolves.

## Driving it from an agent

```bash
uv run prodscrape-mcp          # stdio MCP server, 14 tools
```

Register it with Claude Code, then follow [`SKILL.md`](SKILL.md). The design point: **there
is no LLM client in this codebase.** Stage-2 tier B and the review queue are not "call a
model" — they are `pending_*` tools that hand the agent the ambiguous minority as compact
digests, and `record_*` tools that persist its verdicts to `verdicts.json`. Those verdicts
are replayed on every later run, so a question is never asked twice and a finished run
replays with no model involvement at all.

A 500 KB product page reaches the agent as a ~200-token digest; analytik-jena's entire
tier-B queue was 12 pages / ~2,200 tokens.

## Extraction results

| vendor | devices | held for review | merged groups | structured values |
|---|---|---|---|---|
| analytik-jena.com | 143 | 3 | 1 | 49% |
| binder-world.com | 110 | 0 | 59 | 78% |
| qinstruments.com | 13 | 0 | 0 | 9% |

**266 devices, no duplicate names.**

Holding zero-spec rows for review instead of deleting them paid off directly: the
analytik-jena queue started at 25 rows, and 22 of them turned out to be **real instruments
whose spec tables had been rejected by over-strict size rules** (a single-device spec table
is two rows; `multi X 2500`'s is 2x13). Fixing that recovered 56 devices. Had those rows
been dropped as "not devices", the bug would have been invisible.

"Structured values" is the share of specs parsed into a number, range, dimension, boolean
or comparison; the rest stay as text. **Every spec keeps its raw source string either way**,
so a low rate costs queryability, not information — which is exactly what the `specs` bag
is for. QInstruments scores low because its specs are written as prose
("All microplates according SBS format"), not because extraction failed.

SiLA was detected on all 13 QInstruments devices, which is the connectivity signal the
landscape table exists to capture.

## Known gaps

- **No JS rendering.** Static HTML only; Playwright fallback is specified, not built.
- **Family vs. variant granularity differs by vendor.** analytik-jena publishes families
  at depth 5 (`plasmaquant-ms-series`); BINDER publishes individual models at depth 6
  (`kb-pro-260-with-ich-light-module`). Roll-up rules are still an open decision.
- **Instrument/accessory boundary is unresolved** for autosamplers, sample-introduction
  modules and adapter plates — flagged in the golden labels and in every recipe.
- **Spec-table detection relies on a heading.** QInstruments' BioShake XP has six tables
  but no "Technical Data" heading, so it escalates in Stage 2.
- **24 analytik-jena devices have no specs** — no table on the page passed validation.
  They are emitted with a warning rather than dropped.
- **Prose specs are not parsed.** A value like "Adjustable from -20 °C to 99.9 °C up to
  ..." stays text. Half-reading a value is worse than not reading it.
- **No unit conversion yet.** `pint` is a dependency but units are captured, not converted.
- **`memmert.com` / `hettich-lab.com`** are now reachable in principle via the BFS
  fallback but have not been tested.
