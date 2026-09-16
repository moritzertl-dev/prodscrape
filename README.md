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
| `specs_eav.csv` | the lossless master: one row per (device, attribute), every value with its raw source text |
| `review_queue.csv` | rows that need a human or agent verdict, never silently dropped |
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
uv sync && uv run pytest -q     # 127 tests, fully offline
uv run prodscrape tree <domain>
```

## How it works

Five stages. The first four are deterministic and free; only the last costs anything.

**Discover.** `robots.txt`, then the declared sitemaps, then a probe of the usual paths,
then a bounded breadth-first crawl if none of that works. Real sites fail in every way
imaginable — a sitemap declared but returning 500, a sitemap that returns HTTP 200 with an
error message in the body, a site that only answers on `www`, a catalogue so large that
walking it never terminates — so each of those is detected and reported rather than
silently producing an empty result.

**Shortlist.** The agent sees a path-prefix tree, never a raw URL list. Candidates are
**leaf pages — those with no children, at any depth**, which is the structural difference
between a product page and a category page. A fixed path depth is not used, because
catalogues nest products at several levels.

**Classify.** Structural signals decide the clear-cut majority for free: specification
headings in ten languages, order-code patterns, datasheet links, table shape. Only the
ambiguous minority reaches the agent, as a ~200-token digest rather than a page.

**Extract.** Specification tables are located by the heading above them and validated
before use — a wrong table produces confident, plausible nonsense, so no table is
preferred to the wrong one. Table orientation is detected, not assumed. Every value keeps
its raw source string alongside the parsed form.

**Decide what is a device.** One row per device. Listings collapse only when *every*
differing attribute is non-functional — mains voltage, fuse rating, article number,
bundled software. A functional hardware difference always keeps them apart. Merges record
what was merged and why, so a wrong one is visible rather than buried.

## Two principles it is built on

**Python never guesses; the agent never parses HTML.** Deterministic code owns fetching,
caching, table parsing, device identity and export. The agent supplies judgment — is this
page a product, is this row a device — and nothing else. No tool ever returns a page.

**Judgments are artifacts.** Every verdict the agent gives is written to disk and replayed
on later runs, so a question is never asked twice and a finished run replays with no model
involvement at all.

## Cost

Only the escalated minority reaches the model. For a typical vendor of ~60 product pages
that is a few thousand tokens — roughly **400x cheaper** than sending the pages
themselves, which is the entire reason for the digest architecture.

`estimate_cost` reports the expected spend before a run and `cost_report` the measured
spend after. Both count only what the pipeline hands to the model; the agent's own
conversation context is billed too and is not visible from inside the tool, so treat them
as a floor rather than the whole bill.

## Per-vendor recipes

The first run against a new vendor infers its rules from URL structure and reports what it
guessed, including where it is unsure. Once those rules are confirmed they are saved as a
recipe, and every later run is fully deterministic — no exploration, no model calls.

Recipes are data, not code. A recipe you write in your own data directory overrides one
shipped with the package, so an install can be corrected without touching it. A few are
bundled as worked examples; they are never required, and the tool runs on vendors it has
never seen.

## Honest limits

- **Bot-protected sites cannot be scraped.** Some vendors return 403 to anything that
  isn't a real browser session. The tool retries once identifying as a browser, still
  inside `robots.txt`, then reports the site as unscrapeable rather than pretending it is
  empty.
- **No JavaScript rendering.** Static HTML only. Pages that build their content in the
  browser come back empty, and the tool says so.
- **No PDF datasheet extraction.** Where a vendor publishes a thin page and a detailed
  PDF, only the page is read.
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
