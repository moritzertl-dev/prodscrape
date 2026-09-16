# Product List Scraper — Pipeline Specification

**Goal:** given a manufacturer domain, produce a reviewed, reproducible table of that
manufacturer's instruments. Runs as a Claude agent driving deterministic Python tools,
exposed over MCP.

**Status:** design agreed 2026-09-16. Reference site: analytik-jena.com (TYPO3).

## 0. Decisions

| Decision | Choice |
|---|---|
| Purpose | Market/competitive landscape + device connectivity coverage |
| Row granularity | One row per **device** — collapse only non-functional variants (§1.5) |
| In scope | Instruments / hardware devices only |
| Master storage | EAV (long) — lossless, sparse-friendly |
| Export shape | Core columns + `specs` JSON bag; category profiles are an optional later view |
| Token budget | Hardcode wherever possible; model calls are a last resort |

### Core principle

> **Python never guesses. Claude never parses HTML.**

Deterministic code owns fetching, caching, table parsing, unit normalization, validation
and export. Claude owns judgment: which URL branches are products, is-this-a-device, and
mapping messy vendor spec labels onto schema keys. Every Claude decision is persisted with
its reason and the evidence snippet it used, so a run is auditable and replayable.

### Token economy (first-class requirement)

Ranked cheapest to most expensive; always prefer the highest applicable tier:

1. **Site recipe** (§2) — a frozen per-vendor config. Zero model calls.
2. **Deterministic signals** — structural rules resolve the clear-cut majority.
3. **Compact digests** — if the model must decide, it sees ~500 tokens, not a 520 KB page.
4. **Full page to model** — never; only parsed tables are ever passed.

Tools return **summaries and file paths**, not bulk content. No tool ever returns raw HTML
to the agent.

### Reproducibility contract

1. Every HTTP response is stored content-addressed (`sha256`) with URL + timestamp + status.
   A re-run reads cache; it never silently re-fetches.
2. Every stage writes a JSONL artifact. Stages are resumable and idempotent.
3. Each run writes a manifest: config, tool versions, recipe version, prompt versions.
4. Claude's judgments are artifacts too (`classified.jsonl`, `extracted.jsonl`), so a run can
   be replayed without calling the model again.

## 1. Stages

### Stage 0 — Site profiling
`discover_site(domain) -> SiteProfile`

- Fetch `robots.txt`; honour `Disallow`; read `Sitemap:` directives.
- **If absent, probe defaults**: `/sitemap.xml`, `/sitemap_index.xml`, `/sitemap.xml.gz`,
  `/{locale}/sitemap.xml`. (analytik-jena declares none but serves `/sitemap.xml`.)
- Recurse sitemap indexes to leaf URLs. If no sitemap at all -> bounded BFS crawl.
- Detect CMS platform and i18n scheme; pin one canonical locale.

Output: `urls_raw.jsonl` — `{url, source, lastmod}`.

### Stage 1 — URL triage  (user step 1)
`url_inventory(run_id) -> PrefixTree`

Returns a **path-prefix tree with counts + sample URLs per branch**, never the raw URL list.
This is the context-economy move: Claude reads a ~40-line tree, not 40k URLs.

`select_url_patterns(run_id, include[], exclude[], depth_hint)`

Claude picks include/exclude globs **once**, and they are written to the site recipe.
Cheap structural pre-filters that proved effective on the reference site:
- path depth (families at depth 5; depths 1-4 are taxonomy landing pages)
- slug suffix conventions (`*-series`)
- hard excludes: news, blog, careers, events, `/knowledge/`, `/company/`, PDFs, search, cart

`harvest_listing(url)` — follow category pages + pagination for links the sitemap missed.

Output: `candidates.jsonl` — `{url, discovered_via, anchor_text, depth}`.

### Stage 2 — Binary classification  (user step 2)
Two tiers, because LLM-classifying every page is slow and expensive.

**Tier A — deterministic signals** (`page_signals(url)`), no model call:
- JSON-LD `@type: Product`  *(absent on TYPO3 — do not depend on it)*
- headings matching `Technical Data|Specifications|Technische Daten`
- an `Order Information` table / order-number regex (`\d{3}-\d{5}-\d`)
- datasheet or brochure PDF links
- breadcrumb depth, model-number patterns in `<h1>`
- URL depth + slug suffix from the recipe

A page passing the recipe's confidence threshold is classified **without a model call**.

**Tier B — Claude adjudicates only the ambiguous remainder**, from a compact **page digest**
(title, breadcrumb, h1/h2s, first ~1500 chars, spec-table column headers), batched ~20
pages per call. Decisions feed back into the recipe so the ambiguous set shrinks over time.

Output: `classified.jsonl` — `{url, label, confidence, reason, signals, decided_by}` where
`label ∈ {instrument, accessory, consumable, software, service, category_page, other}`
and `decided_by ∈ {recipe, signals, model}`. Only `instrument` proceeds.

### Stage 3 — Extraction  (user step 3)
`extract_tables(url) -> Table[]` with **orientation detection**: decide whether attribute
names live in row 0 or column 0. analytik-jena is variant-major (variants as rows,
attributes as columns); many vendors are the transpose.

Once the recipe records the spec-table selector and orientation for a vendor, extraction is
purely mechanical. Claude is invoked only to map unseen attribute labels onto schema keys,
and each mapping it makes is cached in the recipe's label alias map.

Output: `extracted.jsonl`, one record per family:

```jsonc
{
  "product_id": "analytik-jena__plasmaquant-ms-series",
  "manufacturer": "Analytik Jena",
  "name": "PlasmaQuant MS Series",
  "category": "icp-ms",                  // controlled vocabulary
  "url": "...", "image_url": "...", "datasheet_urls": ["..."],
  "description": "...",
  "interfaces": ["Ethernet"],            // connectivity = first-class core
  "control_software": ["ASpect MS"],
  "variants": [
    {"name": "PlasmaQuant MS Elite", "order_number": "818-08021-2",
     "specs": {"sensitivity_115In": {"value": 1500, "unit": "kcps/ppb",
                                     "raw": "115In > 1500 kcps/ppb"}}}
  ],
  "specs": { /* attributes shared by all variants, same {value,unit,raw} envelope */ },
  "specs_raw_tables": [ /* verbatim parsed tables, for RAG + audit */ ]
}
```

Every value carries `{value, unit, raw, source_snippet}` — never a bare string. Uniform
envelope, variable payload. The `specs` bag is what a downstream RAG or agent reads when a
question falls outside the core columns.

### Stage 3.5 — Device identity: what earns a row

**One row per device.** Two listings collapse into a single device only when *every*
differing attribute is non-functional: mains voltage, power frequency, plug type, regional
approval, article number, designation. Any difference in what the device can actually do
keeps them apart.

Implemented in `devices.py`, grounded in cached vendor data rather than assumption:

| case | differing attributes | outcome |
|---|---|---|
| BINDER `B028-230V` vs `B028-120V` | Article Number, Designation, Rated Voltage, Power frequency (4 of 26; all 22 functional specs identical) | **1 device** |
| QInstruments `BioShake 3000` vs `3000 elm` | exchangeable magnetic lock; universal 85–264 VAC supply so no regional split exists | **2 devices** |
| analytik-jena `PlasmaQuant MS` / `Elite` / `Elite S` / `Q` | detector sensitivity, cones, roughing pump | **4 devices** |

Measured effect on BINDER: **173 listed variants → 151 devices**, 18 groups merged.

Merging is evidence-backed and reversible. The merged row keeps every source designation
and article number under `regional_variants`, together with the differing values and the
reason it merged, so nothing is silently dropped and a wrong merge is visible in review.

The cosmetic-attribute list is data, not cleverness — extend `COSMETIC_ATTRIBUTE_PATTERNS`
when a vendor names things differently. `FUNCTIONAL_OVERRIDES` protects attributes that
merely contain a cosmetic substring (`operating voltage range` is a capability, not a
regional variant).

### Stage 3.6 — Review queue: what is not a device

A record with **no specification table** is held back from `devices.csv` and written to
`review_queue.csv` instead. On analytik-jena this catches exactly the series and overview
pages — "PQ LC Series", "AOX Autosampler Series", "Hydride Systems" — which are not
devices and were previously polluting the table.

Zero specs is a strong *indicator*, not proof: a real device could be documented without a
table. So these rows are **held for judgment, never deleted**, and carry the evidence
needed to decide (name, category, URL, description, interfaces, datasheet count, reason)
plus empty `verdict` / `verdict_reason` columns for a reviewer or agent to fill in.

This is also the right seam for the remaining open question — the instrument/accessory
boundary (autosamplers, sample-introduction modules). That is genuine judgment, and the
review queue is a compact list (~24 rows for analytik-jena), so a model pass over it costs
a fraction of what per-page classification would.

### Stage 4 — Normalize, validate, export  (user step 4)
- `pint` for units (°C/°F, mL/L, mm/inch, rpm); `pydantic` for core-schema validation.
- Master persisted as EAV: `product_id | attribute | value | unit | raw | source_url`.
- `export_table(run_id, format, category=None)` renders: core columns + `specs` JSON bag;
  or, where a category profile exists, a pivoted wide table.
- The device table describes **devices only**. Counters describing how the scrape went
  (`spec_count`, `variants_merged`, `spec_source`, `warnings`) are provenance and live in
  `extracted.jsonl` and the manifest, never in the deliverable.
- Formats: CSV, XLSX, Parquet, JSON.

## 2. Site recipes — learn once, replay free

`recipes/<domain>.yaml` freezes everything the agent discovered about a vendor:

```yaml
domain: analytik-jena.com
platform: typo3
locale: en
sitemap_urls: ["https://www.analytik-jena.com/sitemap.xml"]   # not in robots.txt
product_urls:
  include: ["/products/**"]
  exclude: ["/knowledge/**", "/company/**", "/industries-solutions/**"]
  family_depth: 5
  slug_suffix: "-series"
classification:
  spec_heading: ["Technical Data"]
  order_heading: ["Order Information"]
  order_number_re: '\d{3}-\d{5}-\d'
extraction:
  spec_table_orientation: variant_major   # variants are rows
  label_aliases:
    "Dimension (width x depth x height)": dimensions
    "Plasma Gas Flow": plasma_gas_flow
version: 1
```

First run against a new vendor: agent explores, proposes a recipe, human approves.
Every run after that: **zero model calls** unless a drift check fails (page structure
changed, or the classified count moves by more than a set tolerance).

## 3. Evaluation — how we know it works

- **Golden set A (classification):** ~40 hand-labelled analytik-jena URLs spanning
  instruments, category pages, accessories, software, news. Metric: precision/recall,
  target ≥0.95 precision on `instrument`.
- **Golden set B (extraction):** ~10 hand-extracted families. Metric: per-field accuracy
  against ground truth.
- **Regression:** golden sets run off the HTTP cache, so tests are offline, deterministic
  and free.
- A second vendor is added later to check the pipeline isn't overfit to TYPO3.

## 4. Repo layout

```
src/prodscrape/
  fetch.py        # httpx + content-addressed cache + robots + rate limit
  discover.py     # stage 0: robots, sitemap probing, BFS fallback
  inventory.py    # stage 1: prefix tree, pattern selection
  signals.py      # stage 2 tier A: deterministic page signals
  digest.py       # stage 2 tier B: compact page digest for Claude
  tables.py       # stage 3: table parsing + orientation detection
  schema.py       # pydantic core schema, EAV store
  normalize.py    # pint units, controlled vocabulary
  recipes.py      # load/validate/apply site recipes
  export.py       # stage 4: CSV/XLSX/Parquet/JSON
  mcp_server.py   # FastMCP tool surface
recipes/          # per-vendor frozen configs
profiles/         # optional per-category views (YAML)
tests/
  golden/         # labelled fixtures + cached HTML
SKILL.md          # step-by-step agent instructions
runs/<run_id>/    # artifacts + manifest
```

## 5. MCP tool surface

Stage-aligned, idempotent, artifact-backed:

`discover_site` · `url_inventory` · `select_url_patterns` · `harvest_listing` ·
`fetch_pages` · `page_signals` · `page_digest` · `record_classification` ·
`extract_tables` · `record_extraction` · `validate_run` · `export_table` ·
`load_recipe` · `save_recipe` · `eval_run`

## 6. Reference-site findings (analytik-jena.com, 2026-09-16)

| Finding | Consequence for the design |
|---|---|
| `robots.txt` declares no sitemap, but `/sitemap.xml` serves a sitemap index | Probing default paths is mandatory |
| 854 URLs total; 92 under `/products` | Whole-site scope is tiny — BFS fallback rarely needed |
| Zero JSON-LD on product pages | Cannot depend on schema.org `@type: Product` |
| Specs fully present in static HTML | Playwright is a conditional fallback, not the default |
| Technical Data table is variant-major | Table orientation must be detected, not assumed |
| Family page carries all variants + order numbers | One fetch per family yields everything |
| Families at path depth 5, `-series` suffix; taxonomy at depths 1–4 | Strong, free structural pre-filter |
| Page is clean UTF-8 (`µ` intact) | Honour declared charset; never decode blind |

## 7. Open questions

- Multi-vendor runs: one run per manufacturer, or a shared cross-vendor master table?
- PDF datasheet extraction: often the only source of full specs. Stretch goal.
- Controlled category vocabulary: adopt vendor taxonomy, or map onto a neutral one
  (needed for cross-vendor landscape comparison).
- Drift detection policy: what change in classified count should force a recipe re-review?
