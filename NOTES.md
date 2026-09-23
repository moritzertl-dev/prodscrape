# Field notes

Quirks found on real vendor sites, and what the tool does about each. The point is to
stop re-discovering them: if a new vendor misbehaves, check here before debugging.

Every entry that is fixed has a test — `tests/test_generalization.py` for the first
generation, `tests/test_discovery_v2.py` and `tests/test_extraction_v2.py` for the second
(Agilent, Azenta, Beckman Coulter, Brooks, Formulatrix, Tecan). The note explains *why* the
rule exists, the test stops it regressing.

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
| **every page** answers 403 behind Akamai (agilent.com) or a Cloudflare JS challenge (beckman.com), even to a browser UA | nothing is readable live | the newest *successful* Internet Archive capture is used instead; each record keeps `source="archive"` and the capture date, and the report says how many pages came from the archive. Bot protection is never circumvented |
| the archive's newest capture of a protected page **is the challenge page** (beckman.com homepage, served with 200) | "latest snapshot" returns "Just a moment…" | challenge bodies are detected by content, and older captures are tried |
| the archive's CDX index takes 20 s+ per query | a 300-page vendor would take hours | fast path first: `/web/<today>id_/<url>` in one request; CDX only when that capture is a challenge |
| the vendor redirects `agilent.com` → `www.agilent.com`, *then* blocks | an archived page without that redirect left discovery on a host with no robots.txt and no sitemap, and it fell back to crawling | an archived record keeps the vendor's own redirect target as its location |
| a 50,000-URL e-shop sitemap is declared **before** the product sitemap (agilent.com `pim_commerce01.xml` vs `products0.xml`) | the URL budget runs out before a single instrument | sitemaps are read in order of their file names: product-like first, commerce/media/community last, other hosts last of all |
| the product sitemap returns an HTML page (azenta.com `az-products-sitemap.xml`) | `/products/*` never appears in the URL list | the products are found from category pages instead (focused crawl) |

## Where the catalogue is

The single biggest generalisation gap. URL structure alone found the right catalogue on
one of six new vendors.

| what happens | handled |
|---|---|
| products are **flat single-segment slugs**, indistinguishable from blog posts (Tecan, Formulatrix, Azenta) | scope is read from the **navigation menu** with its hierarchy (`Products > Microplate readers > Spark®`), not from the URL tree |
| the products live on **another host** (`lifesciences.tecan.com`; the corporate sitemap never mentions it) | sibling hosts linked from the menu have their own menus read too |
| part of the range lives on **other domains** (Brooks → `brookslabautomation.com`, `preciseflexrobots.com`) | off-domain menu links are shown to the scope step as `[external]`; a chosen site gets a second, separate scope call over its own menu |
| the menu is **built in JavaScript** (agilent.com, AEM) | fewer than 15 static menu links → the homepage's body links are used as well |
| a mega-menu links one product once per **tab** (`Fluent® > Overview / Software / Literature`) | links collapse on host+path; the entry whose text is the others' parent label wins |
| the social-link filter `"x.com"` matched **formulatrix.com** as a substring | social hosts are compared as registrable domains |
| the scope decision is a judgment, not a rule | one model call over a ~3-6k token digest (menu + URL-tree summary); a keyword heuristic stands in without a model and says so; the decision is frozen in the recipe |

## Crawling from category pages

| what happens | handled |
|---|---|
| category pages link hundreds of things: citations, journal articles, app notes, downloads (Tecan: ~700 links) | links are **triaged by the model before any fetch** (~15 tokens per link); decisions persist in `triage.json` |
| a catalogue branch holds ~4,000 leaves, mostly columns and spare parts (agilent.com `/en/product/`) | **branch triage**: the model sees parent paths with counts and sample slugs, and whole consumable branches are dropped before per-URL triage |
| a product page linking 56 things was expanded as if it were a listing | model-named product pages are only expanded when their links are triaged |
| `?hsLang=en` on every internal link (HubSpot) doubled the candidate list | tracking parameters are stripped from every URL |
| template bugs render `/null/doc/...` links; all 60 were 404s | `null`/`undefined` path segments are rejected |
| a sitemap keeps old slugs that **redirect** to the current page | one row per final URL |
| a listing continues on `?page=2` / `/page/2/` | followed as more of the same hub |
| the vendor's body class says `mega-menu-header` (Azenta) and chrome stripping removed `<body>` itself — every link on every hub page lost | `html`/`body`/`main`/`article` are never chrome, and the share is measured after the tag strip |

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

| specs written as **`Label: value` text** under a spec heading, no table (Formulatrix: every product) | a section reader: from a spec heading to the next same-level heading; `Label: value` lines are specs, repeated labels are qualified by their sub-heading, unlabelled lines are kept under the sub-heading |
| the spec "heading" is an **accordion button** (`<span>` in `role="button"`) and sub-headings are `<p><strong>` | toggles and bold-only paragraphs count as headings; a toggle only ends a section a toggle opened |
| a **"Resources"** table produced two phantom devices ("Wet Dispense", "Dry Dispense") | tables under resources/downloads/literature/FAQ headings are never spec tables |
| a comparison table read along the wrong axis names **attributes as devices** ("Working Distance"), or **quantities** ("10 µl", "1.5") | entity names must share a word with the product or carry a model designation (letters + digits, not a quantity); otherwise the other axis is tried, and failing that the table is kept as one device |
| **several products on one page** as in-page sections (PreciseFlex robots, `#preciseflex400_labproducts`) | ≥2 same-page anchors whose texts look like a family (shared name or model numbers) split the page into one device per section |
| in-page **tabs** ("Details", "Part Numbers", "Versions") and **FAQ anchors** use the same mechanism | excluded by a UI-word stoplist, the family rule, and "questions are not products" |
| the `<h1>` is a **tagline** ("Step away from manual and repetitive work") | if it shares no word with the menu's name for the page, the menu name is used |
| the same robot on **two pages** | merged when names match and no shared spec disagrees — but a **generic shared title** ("Vibrationssiebmaschine" on every Retsch sieve shaker) never merges without a model number or three agreeing specs |
| datasheet PDFs: a **footer training catalogue** linked from every page was chosen as every device's datasheet | PDFs linked from ≥20% of pages are chrome; file names sharing words with the device name win |
| PDFs served **without an extension** (`/doc/…-brochure-pdf-396116`) behind a **download page** | recognised as documents; one hop from a download page to the file is followed. Tecan's are gated behind a form and stay unread |

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

- **No JavaScript rendering.** Pages built in the browser come back empty. The menu
  fallback (homepage body links) and the archive soften this but do not solve it.
- **PDF extraction is deterministic and conservative.** Ruled tables and `Label: value`
  lines are read; free-layout brochures and gated downloads are not.
- **Marketing-only product pages** (Brooks PathFinder, Beckman discontinued models) have
  no specs anywhere public. They are rows without specs, which is the truth.
- **Brooks semiconductor products** (MagnaTran, Marathon) exist only as headings on
  solution pages, with no page of their own; they are not in the table.
- **Units are captured, not converted.** Cross-vendor numeric comparison needs a
  normalisation pass.
- **Prose specs stay as text.** Deliberate — half-reading a value is worse than not
  reading it — but it limits filtering.
- **`Option model` and `Net weight` are treated as functional**, so they block some
  merges. A weight difference might be a different transformer; splitting visibly beats
  merging wrongly.
