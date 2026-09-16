# Field notes

Quirks found on real vendor sites, and what the tool does about each. The point is to
stop re-discovering them: if a new vendor misbehaves, check here before debugging.

Every entry that is fixed has a test in `tests/test_generalization.py` — the note explains
*why* the rule exists, the test stops it regressing.

## Discovery

| what happens | why it matters | handled |
|---|---|---|
| `robots.txt` declares no sitemap, but `/sitemap.xml` exists | the obvious path is not the declared path | probe the usual locations regardless |
| a declared sitemap returns **HTTP 200 with an error message** in the body | parses to zero URLs and looks exactly like an empty site | responses are validated as sitemaps before being trusted |
| a declared sitemap returns **500** | the site is fine; only the sitemap is broken | falls through to the crawl |
| `robots.txt` itself **404s** | nothing to read, site still scrapeable | probe, then crawl |
| the bare host **404s without redirecting**; only `www` serves the site | following redirects never finds it | the other host variant is tried explicitly |
| the site returns **403 to any non-browser agent** | genuinely unscrapeable without a browser session | retry once as a browser (still inside `robots.txt`), then report it as blocked rather than empty |
| several sitemap indexes, one of them a huge consumables catalogue | walking them never terminates | URL budget, stopping loudly |
| no sitemap at all | crawl is the only option | bounded BFS, ordered so catalogue paths are visited before news and legal |

## URL structure

| what happens | handled |
|---|---|
| products sit at **several path depths** | candidates are leaf pages — no children, at any depth. A fixed depth drops products above and below it *and* admits categories that sit at it |
| path segments are **translated per locale** (`/bg/products/` but `/de/produkte/`) | locale and catalogue root are chosen jointly, never independently |
| the biggest language branch is not the one you want | inference reports its guess; pin `locale` in the recipe |
| more than one catalogue root | inference returns one — read the tree and add the others by hand |
| the same page published once per SKU (`?part-number=…`) | query-string variants collapse onto one canonical URL, with the variants recorded |

## Page markup

| what happens | handled |
|---|---|
| specs in a **`<dl>`** rather than a `<table>` | definition lists are read as two-column spec tables |
| spec table is **variant-major** (variants as rows) or **attribute-major** | orientation is detected, never assumed — getting it wrong transposes every value |
| the header row is **blank**, with variant names in a later `Designation` row | detected, and names recovered from that row |
| a **single-device** spec table is only two rows | size bars are relaxed under a spec heading |
| downloads / order / cookie tables look structurally like spec tables | rejected by structure: a language-code column, a file-size column, or non-spec header words |
| a spec attribute is legitimately called "Sample size" or "Format" | weak header words only reject a *narrow* table carrying two or more of them |
| accessory catalogues of 100+ rows | rejected by variant count and order-code columns |

## Text extraction

| what happens | handled |
|---|---|
| nav and footer mention interfaces the product lacks | chrome is stripped before interfaces are read |
| a wrapper class contains "header" and holds the whole article | elements carrying a large share of the page text are never stripped, plus a guard that falls back to the full body if stripping removes too much |
| **"Digital in PDF format"** read as a digital I/O interface | interface patterns require the full word (`inputs`, `outputs`, `i/o`), not `in`/`out` |
| ISO dates match the order-code pattern | dates are filtered out explicitly |
| `µ`, `°`, `±` in specs | declared charset is honoured; never decode blind |
| marketing tagline nested inside `<h1>` | only the direct text nodes are the name |
| trailing order code in a variant name | stripped, but only with two or more hyphen groups so `BD056-230V` survives |
| configuration descriptors separated by `;` | **not** split on — doing so merged two different devices into one name |

## Device identity

| what happens | handled |
|---|---|
| the same machine in 230 V and 120 V builds | one device; mains voltage, frequency, fuse rating and article number are non-functional |
| fuse rating differs because 120 V draws double the current | treated as a consequence of the build, not a capability |
| a touchscreen variant lists **identical specs** | the name is the only evidence — a name difference surviving regional-token stripping blocks the merge |
| an added hardware module, or a different orbit diameter | separate devices; only functional *hardware* differences count |
| different bundled software or warranty | one device |

## Things that are still open

- **No JavaScript rendering.** Pages built in the browser come back empty.
- **No PDF datasheet extraction.** A thin page plus a detailed PDF yields only the page.
- **Units are captured, not converted.** Cross-vendor numeric comparison needs a
  normalisation pass.
- **Prose specs stay as text.** Deliberate — half-reading a value is worse than not
  reading it — but it limits filtering.
- **`Option model` and `Net weight` are treated as functional**, so they block some
  merges. A weight difference might be a different transformer; splitting visibly beats
  merging wrongly.
