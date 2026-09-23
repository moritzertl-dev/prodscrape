---
name: scrape-product-catalogue
description: Build a reproducible table of a lab-instrument manufacturer's devices from their website. Use when asked to catalogue, list, or compare a vendor's products, or to extract device specifications and connectivity from a manufacturer site.
---

# Scraping a vendor product catalogue

This skill drives the `prodscrape` MCP tools. **If those tools are not available, say so
and stop** — nothing here can be done without them, and a table written from memory is
not a scrape.

The tools do all the work, including cost accounting. Your job is to call them in the
order below and relay what they return. Do not improvise steps, and never produce a
token or cost figure of your own.

## Procedure

**1. Call `catalogue_vendor(domain, manufacturer)`.** It starts the whole run —
discovery, scope, crawl, classification, extraction, datasheets, review, automation
screen — as a background process and answers within a minute.

While the answer says `"state": "running"`, call `catalogue_status(domain)` again. Each
call waits up to 40 s. Between calls, tell the user the `stage` in one short line
("crawling: 120 pages fetched"), nothing more. A typical vendor takes 2-10 minutes.
Do not write recipes, split the scan, or call stage tools to work around the time
limit — the background run exists so you never have to.

If the state is `failed` or `stalled`, quote the log lines it returns and call
`catalogue_vendor` again once; it resumes from the cache.

**2. When `state` is "done", read `next_step` and do exactly what it says.** There are three cases.

- `done — ...` → go to step 3.
- `call pending_classifications ...` → call `pending_classifications(domain)`. It
  returns `instructions` and a list of `digests`. Label each digest by following the
  `instructions` text, then send every verdict to
  `record_classifications(domain, verdicts=[{url, label, reason}])`. Repeat until
  `pending_total` is 0, then call `catalogue_vendor` again with the same arguments
  and follow step 1.
- `call pending_reviews ...` → call `pending_reviews(domain)`. For each row decide
  `device` or `not-a-device`, then send them to
  `record_reviews(domain, verdicts=[{product_id, verdict, reason}])`. Then call
  `catalogue_vendor` again.

This loop only happens when no model backend is configured on the server. When one is
configured, the background run finishes on its own.

**3. Call `open_device_table(domain)`.** It opens the table in the browser.

**4. Reply with `report_to_user`, copied verbatim.** It already contains the device
count, what is held for review, the measured model usage and cost, the scope decision,
and every caveat (archived pages, page budget reached, heuristic scope). Add nothing
before it. After it, add at most one sentence, and only if something went wrong that
the report does not already say.

## Rules

- **Never estimate cost yourself.** The report's figures are measured by the server:
  every model call, and every tool result handed to you. If the user asks what a run
  will cost *before* running it, call `estimate_cost(domain)` and quote its `summary`.
  If it returns no estimate, say there is no basis yet.
- **Never print the device table into the chat.** `open_device_table` shows it.
  `device_table` and `device_specs` exist for when the user asks a question about
  specific devices.
- **Labels for classification:** `instrument`, `accessory`, `consumable`, `software`,
  `service`, `category_page`, `other`. Only `instrument` becomes a row. Always give a
  one-line `reason`; it is stored and is how a wrong call gets found later.
- **Do not fetch vendor pages yourself** or reason from page HTML. If a result looks
  wrong, report what looks wrong, and name the device or URL.

## Other tools (only when asked)

| tool | use |
|---|---|
| `cost_report(domain)` | measured usage for a vendor, all runs to date |
| `device_table(domain)` / `device_specs(domain, name)` | answer questions about specific devices |
| `export_table(domain, category)` | per-category wide table |
| `run_status(domain)` | what has been done for a vendor |
| `site_overview`, `put_recipe`, `scan_site`, `extract_devices` | manual control of single stages, for debugging a vendor |
| `storage_paths()` | where runs, caches and recipes live |
