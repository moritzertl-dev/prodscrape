---
name: scrape-product-catalogue
description: Build a reproducible table of a lab-instrument manufacturer's devices from their website. Use when asked to catalogue, list, or compare a vendor's products, or to extract device specifications and connectivity from a manufacturer site.
---

# Scraping a vendor product catalogue

You drive the `prodscrape` MCP tools. The Python side does everything deterministic —
fetching, caching, table parsing, device identity, export. **You supply judgment only
where structure cannot decide**, and your judgments are written to disk so they are never
asked twice.

## The one rule that matters

**Never ask for a page.** No tool returns HTML, and you should never want it to. The
largest thing you will see is a ~200-token page digest. A single product page is 500 KB;
the whole tier-B queue for a vendor is about 2,000 tokens. If you find yourself wanting
raw page content, the answer is a better deterministic rule in the recipe, not a bigger
context window.

## Procedure

### 0. State the expected cost — `estimate_cost(domain)`

After the first scan, before you start judging anything, report what the run is expected
to cost and say so in one line. At the end, call `cost_report(domain)` and report what it
actually cost. Both figures count **only what the pipeline hands to the model** — your own
conversation context is billed too and is not visible to these tools, so present them as a
floor, never as the whole bill.

### 1. Look at the site — `site_overview(domain)`

A handful of requests, no product pages. Read the returned prefix tree and check:

- `total_urls` is non-zero. If it is zero the site has no usable sitemap and no crawlable
  links; say so rather than guessing.
- `robots_readable` is true. If false, the sitemap fields are **not trustworthy** — a
  failed robots fetch is a fault in the run, not a fact about the site.
- `discovered_via_crawl` tells you the sitemap was missing or broken and a bounded crawl
  was used instead.
- If `saved_recipe` is present, skip to step 3. The vendor is already understood.

### 2. Write the recipe — `put_recipe(...)`

`suggested_rules` is a deterministic guess from URL structure. **Verify it against the
tree before saving**, because it is wrong in predictable ways:

- **Locale.** It picks the language branch with the most URLs, which is often not English.
  BINDER's guess was `de-de`; the right answer was `int-en`.
- **Missed branches.** It returns one catalogue root. QInstruments has two — `/automation`
  and `/laboratory` — and the guess found only the first.
- **No product token.** If the notes say "verify this", the site has no `/products/` path
  and the guess is a largest-branch fallback. Read the tree yourself.
- **Leave `leaf_only` on.** Candidates are pages with no children, at any depth — that
  is what a product page is, structurally. Do not replace it with a `family_depth`:
  catalogues nest products at several levels, and a single depth fails both ways, by
  dropping products above and below it and by admitting category pages that happen to
  sit at it. On BINDER a fixed depth was silently skipping 40 real product pages.
- **`family_depth`** is only a scoring hint now. Set `family_depths` (a list) if a
  vendor genuinely needs an explicit whitelist of levels.

Add `spec_headings` if the vendor words it unusually. These *extend* the built-in defaults
(`technical data`, `specifications`, `technische daten`), so a partial recipe is always
safe.

A saved recipe is what makes every later run free.

### 3. Scan — `scan_site(domain)`

Fetches candidates and classifies them from structural signals. Returns counts only.
Be patient: first run is rate-limited at ~1s per page; re-runs read cache and take seconds.

### 4. Judge the leftovers — `pending_classifications` → `record_classifications`

This is your first real job. You get digests for pages the signals could not decide,
typically 10-25% of candidates.

Label each: `instrument`, `accessory`, `consumable`, `software`, `service`,
`category_page`, `other`. Only `instrument` proceeds.

Guidance from real cases:

- "Disposable Tips for X" → `consumable`
- An interchangeable pipetting head or a sample-introduction module → `accessory`
- "Automated ELISA", "Automated NGS Library Preparation" → `other`; these are application
  and workflow pages, not products
- A portfolio landing page → `category_page`
- An integrated workcell → `instrument`; it is hardware

**Always give a `reason`.** It is stored and is what makes a wrong call reviewable later.

Then call `scan_site` again to fold the verdicts in, and confirm `pending_total` is 0.

### 5. Extract — `extract_devices(domain, manufacturer)`

Fully offline. Produces one row per device, merging variants that differ only in
non-functional ways (mains voltage, fuse rating, article number, bundled software).
A functional *hardware* difference always makes a separate device.

### 6. Settle the review queue — `pending_reviews` → `record_reviews`

Records with no extracted specifications. **Read these carefully: most are usually real
devices whose spec table failed to parse, not junk.** On analytik-jena the queue started
at 25 rows, of which 22 were genuine instruments with unparsed tables.

So before recording `not-a-device` verdicts, ask whether the name looks like a real
instrument. If a queue is large and full of plausible device names, that is a **parsing
bug to report, not a backlog to adjudicate**. Verdict `device` promotes the row into the
table even without specs.

### 7. Deliver — `device_table`, `device_specs`, `export_table`

`export_table(domain, category=...)` also writes a pivoted per-category view, promoting
that category's common attributes into real columns. It reads the same extracted data, so
it never needs a re-scrape.

## Reading the output honestly

- `devices.csv` — one row per device: core columns plus a `specs` JSON bag. **Devices
  only**; no scrape metadata.
- `specs_eav.csv` — the lossless master, one row per (device, attribute), every value with
  its raw source text.
- `review_queue.csv` — held for judgment, never deleted.
- `extracted.jsonl` — everything, including provenance and merge reasons.

Every spec keeps its `raw` string. A low structured-parse rate means values are written as
prose, **not** that extraction failed — QInstruments parses at 9% and the data is complete.
Report it that way.

## What to tell the user

Give counts, the review-queue size, and anything you could not resolve. Do not present a
number as verified when it rests on a rule you guessed at — say which recipe fields you
inferred and which you confirmed against the tree.

Close with the cost: the estimate you gave at the start and the measured figure from
`cost_report`, stated as tokens and dollars, with the caveat that it excludes your own
context. If the two diverge a lot, say why — usually the escalation rate for this vendor
differed from the 30% planning assumption.
