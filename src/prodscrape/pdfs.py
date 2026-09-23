"""Stage 3b — datasheet PDFs, for devices whose web page is thin.

Many vendors publish a marketing page and put the real specifications in a PDF: of the
six vendors this was generalised on, Formulatrix, Azenta and Brooks keep most numbers out
of their HTML entirely. Reading only the page left those devices with no specs and sent
them to the review queue as if they were overview pages.

Deterministic throughout — no model reads a PDF:

1. **Pick the PDF.** Only for devices with fewer than ``THIN`` specs. Links are scored
   by file name and link text: "datasheet", "specification", "technical data" win;
   manuals, safety data sheets, certificates and application notes are never read.
2. **Read tables.** ``pdfplumber`` recovers ruled tables; each is put through the same
   validation as an HTML table (not a downloads list, not an order list, sane shape).
3. **Read key-value lines.** Datasheets without ruled tables lay specs out as
   ``Label ....... value`` or ``Label: value``; a line qualifies only when the value
   carries a digit, which keeps prose out.

Everything found is stored per page in ``datasheet_specs.json`` and merged by
``run_extract`` into devices only where the page has a single device and the key is not
already known — the page always wins, and each merged value carries
``source="datasheet:<url>"``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from .extract import _is_order_list, _looks_like_non_spec
from .fetch import BLOCKED_STATUSES, DEFAULT_DELAY, Cache, Fetcher
from .navigation import registrable_domain
from .paths import cache_dir, runs_dir
from .tables import to_spec_table

THIN = 5                        # a device with fewer specs than this gets a datasheet read
MAX_PDF_BYTES = 15_000_000
MAX_PAGES = 8                   # datasheets front-load specs; brochures run long
MAX_PDFS_PER_RUN = 150

GOOD = re.compile(r"data[\s_-]?sheet|datenblatt|spec(?:ification)?s?|technical|tech[\s_-]?data|"
                  r"product[\s_-]?(?:note|sheet|info)|brochure|flyer|leaflet", re.I)
BAD = re.compile(r"manual|handbuch|instruction|ifu|sds|msds|safety|certificat|declaration|"
                 r"conformity|app(?:lication)?[\s_-]?note|poster|case[\s_-]?stud|white[\s_-]?paper|"
                 r"press|price|terms|warranty|coa\b|recycl|policy|quick[\s_-]?(?:start|guide)|"
                 r"install|release[\s_-]?notes|webinar|citation|publication|journal|"
                 r"catalog(?:ue)?|training|obsolescence|letter|newsletter|flyer-event", re.I)

# "Temperature range ........ 4 - 42 °C", "Weight: 12 kg", "Dimensions (W x D x H)  45 x 60 x 30 cm"
_KV_RE = re.compile(
    r"^\s*(?P<label>[A-Za-zÄÖÜäöüß][^:\n]{1,48}?)\s*(?::|\.{3,}|\t|\s{3,})\s*(?P<value>.{1,80})$"
)
_DIGIT = re.compile(r"\d")


def score_pdf(url: str, text: str = "") -> int:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    hay = f"{name} {text}"
    if BAD.search(hay):
        return -1
    return 2 if GOOD.search(hay) else 0


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3}


def choose_datasheet(
    urls: list[str], vendor_domain: str, *, device_name: str = "",
    chrome: set[str] | frozenset = frozenset(),
) -> str | None:
    """The PDF most likely to be this device's datasheet, or None.

    A PDF whose file name shares words with the device ("infinite-200-pro-...-brochure")
    beats a generic one, and a PDF linked from a large share of the site's pages is
    chrome, never a datasheet — Tecan links a training catalogue from every footer.
    """
    name_words = _words(device_name) - {"the", "and", "for", "system", "series"}
    ranked = []
    for i, u in enumerate(urls):
        if u in chrome:
            continue
        score = score_pdf(u)
        if score < 0:
            continue
        same = registrable_domain(urlparse(u).netloc) == registrable_domain(vendor_domain)
        overlap = len(name_words & _words(unquote(urlparse(u).path)))
        ranked.append((score + (1 if same else 0) + min(2 * overlap, 4), -i, u))
    if not ranked:
        return None
    best = max(ranked)
    # An unnamed PDF is only worth reading when it is the only candidate on the page.
    return best[2] if best[0] >= 1 or len(ranked) == 1 else None


def _pdf_link_in(html: str, page_url: str) -> str | None:
    """The file behind a download page: a meta refresh, or the first real .pdf link."""
    from urllib.parse import urljoin

    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    meta = tree.css_first('meta[http-equiv="refresh" i]')
    if meta is not None:
        m = re.search(r"url=(.+)$", meta.attributes.get("content") or "", re.I)
        if m and ".pdf" in m.group(1).lower():
            return urljoin(page_url, m.group(1).strip("'\" "))
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if re.search(r"\.pdf(\?|$)", href, re.I):
            return urljoin(page_url, href)
    return None


def _fetch_pdf(fetcher: Fetcher, url: str, store: Path) -> bytes | None:
    store.mkdir(parents=True, exist_ok=True)
    path = store / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".pdf")
    if path.exists():
        data = path.read_bytes()
        return data or None
    if not fetcher.allowed(url):
        return None
    fetcher._throttle(url)
    try:
        resp = fetcher._client.get(url, timeout=60.0)
        if resp.status_code in BLOCKED_STATUSES:
            from .fetch import BROWSER_USER_AGENT
            resp = fetcher._client.get(url, headers={"User-Agent": BROWSER_USER_AGENT},
                                       timeout=60.0)
    except Exception:
        return None
    # Document libraries often answer with a download *page* rather than the file
    # (Tecan: /doc/infinite-f50-family-brochure-pdf-396116 is HTML). Follow one hop to
    # the PDF it links; never more.
    if resp.status_code == 200 and resp.content[:5] != b"%PDF-" and \
            "html" in resp.headers.get("Content-Type", "").lower():
        target = _pdf_link_in(resp.text, str(resp.url))
        if target:
            fetcher._throttle(target)
            try:
                resp = fetcher._client.get(target, timeout=60.0)
            except Exception:
                return None
    ok = (resp.status_code == 200 and resp.content[:5] == b"%PDF-"
          and len(resp.content) <= MAX_PDF_BYTES)
    data = resp.content if ok else b""
    path.write_bytes(data)          # an empty file records a failed fetch; no retry
    return data or None


def parse_pdf(data: bytes) -> dict[str, str]:
    """Attribute -> raw value from a datasheet. Tables first, then key-value lines."""
    import pdfplumber

    specs: dict[str, str] = {}
    lines: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:MAX_PAGES]:
                for table in page.extract_tables() or []:
                    grid = [[" ".join((c or "").split()) for c in row] for row in table if row]
                    grid = [r for r in grid if any(r)]
                    if len(grid) < 2 or len(grid[0]) < 2:
                        continue
                    width = max(len(r) for r in grid)
                    grid = [r + [""] * (width - len(r)) for r in grid]
                    if _looks_like_non_spec(grid) or _is_order_list(grid):
                        continue
                    if width == 2:
                        # The classic datasheet table: parameter | value.
                        for label, value in grid:
                            if label and value and len(label) <= 60:
                                specs.setdefault(label, value)
                        continue
                    table_ = to_spec_table(grid)
                    if len(table_.entities) == 1:
                        for attr, value in next(iter(table_.records.values())).items():
                            if attr.strip() and value.strip():
                                specs.setdefault(attr.strip(), value.strip())
                text = page.extract_text() or ""
                lines += text.splitlines()
    except Exception:
        return specs

    for line in lines:
        m = _KV_RE.match(line)
        if not m:
            continue
        label, value = m.group("label").strip(" .:"), m.group("value").strip(" .")
        if not _DIGIT.search(value) or len(label.split()) > 7:
            continue
        if _DIGIT.match(label):          # "2 x ..." is a sentence fragment, not a label
            continue
        specs.setdefault(label, value)
    return specs


def enrich_with_datasheets(domain: str, *, delay: float = DEFAULT_DELAY) -> dict:
    """Read datasheets for thin devices of a finished extraction. Returns counts."""
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    out = runs_dir() / domain
    extracted = out / "extracted.jsonl"
    if not extracted.exists():
        return {"error": "no extraction yet"}
    records = [json.loads(l) for l in extracted.read_text(encoding="utf-8").splitlines()
               if l.strip()]
    per_page: dict[str, list[dict]] = {}
    for r in records:
        per_page.setdefault(r["url"], []).append(r)

    store_path = out / "datasheet_specs.json"
    known = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}

    # Site chrome: a PDF linked from a fifth of the pages (and at least three).
    from collections import Counter
    freq = Counter(u for recs in per_page.values() for u in set(recs[0].get("datasheet_urls", [])))
    chrome = {u for u, n in freq.items() if n >= 3 and n >= 0.2 * len(per_page)}

    tried = read = enriched = recovered = 0
    started = time.time()
    with Fetcher(Cache(cache_dir() / domain), delay=delay) as fetcher:
        for url, recs in per_page.items():
            if url in known or tried >= MAX_PDFS_PER_RUN:
                continue
            # Multi-device pages keep their per-device table; one datasheet cannot be
            # attributed to one of several devices.
            if len(recs) != 1 or len(recs[0]["specs"]) >= THIN:
                continue
            pdf_url = choose_datasheet(recs[0].get("datasheet_urls", []), domain,
                                       device_name=recs[0]["name"], chrome=chrome)
            if pdf_url is None:
                continue
            tried += 1
            data = _fetch_pdf(fetcher, pdf_url, cache_dir() / domain / "pdf")
            specs = parse_pdf(data) if data else {}
            known[url] = {"pdf": pdf_url, "specs": specs}
            if data:
                read += 1
            if specs:
                enriched += 1
                recovered += 1 if not recs[0]["specs"] else 0
    store_path.write_text(json.dumps(known, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "pdfs_tried": tried,
        "pdfs_read": read,
        "devices_enriched": enriched,
        "records_recovered": recovered,
        "seconds": round(time.time() - started, 1),
    }


def load_datasheet_specs(run_dir: Path) -> dict:
    path = Path(run_dir) / "datasheet_specs.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
