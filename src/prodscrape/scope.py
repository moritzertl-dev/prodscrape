"""Stage 1 — where on this vendor's web presence do its instruments live?

This was the least general part of the tool. The old rule looked for a ``/products/``
path segment and took the biggest branch under it; across six new vendors it was right
once. It picked Azenta's blog, found nothing on Formulatrix, missed that Tecan's products
live on a different host, and could not see that Brooks' lab products live on two other
domains entirely.

The replacement reads what a human would: the navigation menu, with its hierarchy
(``Products > Microplate readers > Spark®``), the links on the homepage when the menu is
built in JavaScript, and a count-summarised URL tree from the sitemap. One model call
turns that ~3-6k-token digest into a scope decision:

* ``products`` — menu entries that are individual instrument pages
* ``hubs`` — category and listing pages whose products may not all be in the menu
* ``catalogue_prefixes`` — URL-tree branches that hold instrument pages

Without a model, a deterministic heuristic over the same menu makes a weaker version of
the same decision and says so. Either way the decision is frozen into the recipe, so a
vendor's scope is judged once and replayed for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse

from .fetch import Fetcher
from .inventory import prefix_tree
from .llm import Reasoner
from .navigation import (
    NavEntry, content_links, extract_nav, registrable_domain, same_vendor,
)

# Sibling hosts that never hold a catalogue. Everything else linked from the menu under
# the vendor's registrable domain gets its homepage menu read as well.
NON_CATALOGUE_HOSTS = re.compile(
    r"^(?:careers?|jobs?|investors?|ir|account|accounts|login|my|sso|auth|community|"
    r"forum|academy|training|learn|events?|news|blog|media|press|cdn|static|assets|"
    r"images?|img|sms-ext|monitor|portal|status|mail|email|go|info|pages|lp|survey|"
    r"manuals?|docs?|support|help|kb|store|estore|shop|ecommerce|webshop|web)\.",
    re.I,
)
MAX_SIBLING_HOSTS = 4
MAX_DIGEST_ENTRIES = 450
MIN_NAV_ENTRIES = 15
MAX_EXTERNAL_ENTRIES = 25

# Deterministic fallback vocabulary.
_PRODUCT_TRAIL = re.compile(
    r"\b(products?|instruments?|equipment|systems|devices|hardware|portfolio|"
    r"produkte|geräte|produits|productos|prodotti)\b", re.I,
)
_NOT_INSTRUMENT = re.compile(
    r"\b(consumables?|reagents?|kits?|supplies|software|services?|support|applications?|"
    r"training|literature|downloads?|news|events?|careers?|about|investors?|contact|"
    r"webinars?|blog|resources?|case stud(?:y|ies)|industr(?:y|ies)|markets?|"
    r"parts|accessories|tubes|plates|tips|labware|selector|calculator|request|quote)\b",
    re.I,
)


@dataclass
class ScopeEntry:
    url: str
    name: str
    category: str = ""       # the menu label above it, e.g. "Microplate readers"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScopeDecision:
    products: list[ScopeEntry] = field(default_factory=list)
    hubs: list[ScopeEntry] = field(default_factory=list)
    catalogue_prefixes: list[str] = field(default_factory=list)
    exclude_prefixes: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    decided_by: str = "heuristic"         # model | heuristic | recipe
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "products": [e.as_dict() for e in self.products],
            "hubs": [e.as_dict() for e in self.hubs],
            "catalogue_prefixes": self.catalogue_prefixes,
            "exclude_prefixes": self.exclude_prefixes,
            "hosts": self.hosts,
            "decided_by": self.decided_by,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ScopeDecision":
        return cls(
            products=[ScopeEntry(**e) for e in raw.get("products", [])],
            hubs=[ScopeEntry(**e) for e in raw.get("hubs", [])],
            catalogue_prefixes=list(raw.get("catalogue_prefixes", [])),
            exclude_prefixes=list(raw.get("exclude_prefixes", [])),
            hosts=list(raw.get("hosts", [])),
            decided_by=raw.get("decided_by", "recipe"),
            notes=list(raw.get("notes", [])),
        )

    def merge(self, other: "ScopeDecision") -> None:
        seen = {e.url for e in self.products} | {e.url for e in self.hubs}
        self.products += [e for e in other.products if e.url not in seen]
        self.hubs += [e for e in other.hubs if e.url not in seen]
        for attr in ("catalogue_prefixes", "exclude_prefixes", "hosts", "notes"):
            mine = getattr(self, attr)
            mine += [x for x in getattr(other, attr) if x not in mine]
        if other.decided_by == "model":
            self.decided_by = "model"


@dataclass
class SiteContext:
    domain: str
    base_host: str
    entries: list[NavEntry] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    hosts_read: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- gather

def _homepage(fetcher: Fetcher, url: str) -> str | None:
    try:
        rec, html = fetcher.get(url)
    except Exception:
        return None
    if rec.status >= 400 or "<a" not in html:
        return None
    return html


def gather_context(
    domain: str, base_url: str, fetcher: Fetcher, sitemap_urls: list[str]
) -> SiteContext:
    """Menus of the homepage and of the vendor's catalogue-capable sibling hosts."""
    base_host = urlparse(base_url).netloc
    ctx = SiteContext(domain=domain, base_host=base_host, sitemap_urls=sitemap_urls)
    seen: set[str] = set()

    def add(entries: list[NavEntry]) -> None:
        for e in entries:
            key = urlparse(e.url).netloc + urlparse(e.url).path.rstrip("/")
            if key not in seen:
                seen.add(key)
                ctx.entries.append(e)

    home = _homepage(fetcher, base_url.rstrip("/") + "/")
    if home is None:
        ctx.notes.append("homepage unreadable; scope rests on the sitemap alone")
        return ctx
    ctx.hosts_read.append(base_host)
    nav = extract_nav(home, base_url + "/", domain=domain, include_external=True)
    add([e for e in nav if e.region != "external"])
    # Off-domain menu links go last and are capped: they matter rarely (Brooks' lab
    # automation lives on brookslabautomation.com) but then decisively.
    add([e for e in nav if e.region == "external"][:MAX_EXTERNAL_ENTRIES])
    nav = [e for e in nav if e.region != "external"]
    # A menu assembled in the browser (agilent.com's AEM site) leaves a handful of
    # static links. The homepage body still links the main categories.
    if len(nav) < MIN_NAV_ENTRIES:
        add(content_links(home, base_url + "/", domain=domain))
        ctx.notes.append(
            f"only {len(nav)} menu links in static HTML (menu likely built in "
            f"JavaScript); homepage body links added"
        )

    siblings: list[str] = []
    for e in list(ctx.entries):
        if e.region == "external":
            continue
        host = urlparse(e.url).netloc.lower()
        if host == base_host or host in siblings or NON_CATALOGUE_HOSTS.match(host):
            continue
        if same_vendor(host, domain):
            siblings.append(host)
    for host in siblings[:MAX_SIBLING_HOSTS]:
        page = _homepage(fetcher, f"https://{host}/")
        if page is None:
            continue
        ctx.hosts_read.append(host)
        add(extract_nav(page, f"https://{host}/", domain=domain))
    if len(siblings) > MAX_SIBLING_HOSTS:
        ctx.notes.append(f"{len(siblings) - MAX_SIBLING_HOSTS} sibling hosts not read")
    return ctx


def _url_tree(urls: list[str], base_host: str, limit: int = 70) -> str:
    """The sitemap as branches with counts — only branches big enough to matter."""
    by_host: dict[str, list[str]] = {}
    for u in urls:
        by_host.setdefault(urlparse(u).netloc, []).append(u)
    lines: list[str] = []
    for host, host_urls in sorted(by_host.items(), key=lambda kv: -len(kv[1]))[:3]:
        branches = [b for b in prefix_tree(host_urls, max_depth=3) if b.count >= 3]
        branches.sort(key=lambda b: (-b.count, b.prefix))
        keep = sorted(branches[:limit], key=lambda b: b.prefix)
        prefix = "" if host == base_host else host
        lines.append(f"{host} ({len(host_urls)} URLs)")
        lines += [f"  {prefix}{b.prefix}/  [{b.count}]" for b in keep]
    return "\n".join(lines)


def render_digest(ctx: SiteContext) -> str:
    entries = ctx.entries[:MAX_DIGEST_ENTRIES]
    lines = [f"Vendor domain: {ctx.domain}   main host: {ctx.base_host}", ""]
    lines.append("NAVIGATION (id | menu trail > label -> host/path; host omitted = main host)")
    for i, e in enumerate(entries):
        parsed = urlparse(e.url)
        host = "" if parsed.netloc == ctx.base_host else parsed.netloc
        where = f" [{e.region}]" if e.region != "nav" else ""
        lines.append(f"{i} | {e.label}{where} -> {host}{parsed.path or '/'}")
    if len(ctx.entries) > MAX_DIGEST_ENTRIES:
        lines.append(f"... {len(ctx.entries) - MAX_DIGEST_ENTRIES} more entries omitted")
    if ctx.sitemap_urls:
        lines += ["", "SITEMAP URL TREE (branch [page count])", _url_tree(
            ctx.sitemap_urls, ctx.base_host)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- decide

SCOPE_SYSTEM = """\
You map a manufacturer's website to the part of it that lists their INSTRUMENTS —
physical devices: analyzers, readers, robots, liquid handlers, centrifuges, incubators,
storage systems, workstations, and modules sold as devices.

NOT instruments: consumables, reagents, kits, labware, spare parts, software-only
products, services, applications/workflows/industries, literature, support, news,
company pages, calculators, selectors, request forms.

You receive the site's navigation as numbered entries and a sitemap URL tree with page
counts. Reply with one JSON object and nothing else:
{
  "products": [ids of entries that are an individual instrument or instrument-family page],
  "hubs": [ids of entries that are category or listing pages of instruments, whose
           products may not all appear in the menu],
  "catalogue_prefixes": ["/path/ branches from the URL tree that hold instrument pages;
                          write host/path/ for a host other than the main host"],
  "exclude_prefixes": ["/path/ branches inside those that hold only non-instruments"],
  "notes": "one sentence on anything unusual"
}
Rules: a page for one named device family (e.g. "Spark®", "CytoFLEX S") is a product;
a page grouping several families (e.g. "Microplate readers") is a hub. Include a hub
whenever instruments may sit below it. Use catalogue_prefixes only for branches whose
pages are mostly instruments; leave it empty when the URL tree does not separate them.
Include products on other hosts of the same vendor when the menu links them. Entries
marked [external] point to other domains: choose one only when the menu presents it as
part of this vendor's own product range (a subsidiary or product-line site), never a
distributor, partner or reference."""


def decide_with_model(
    ctx: SiteContext, reasoner: Reasoner, *, preamble: str = ""
) -> ScopeDecision:
    entries = ctx.entries[:MAX_DIGEST_ENTRIES]
    digest = render_digest(ctx)
    if preamble:
        digest = f"{preamble}\n\n{digest}"
    reply = reasoner.ask_json(
        "scope", SCOPE_SYSTEM, digest, items=len(entries), effort="medium",
    )

    def pick(ids) -> list[ScopeEntry]:
        out = []
        for i in ids or []:
            try:
                e = entries[int(i)]
            except (ValueError, IndexError, TypeError):
                continue
            out.append(ScopeEntry(url=e.url, name=e.text, category=_category_of(e)))
        return out

    decision = ScopeDecision(
        products=pick(reply.get("products")),
        hubs=pick(reply.get("hubs")),
        catalogue_prefixes=[p for p in reply.get("catalogue_prefixes", []) if isinstance(p, str)],
        exclude_prefixes=[p for p in reply.get("exclude_prefixes", []) if isinstance(p, str)],
        decided_by="model",
        notes=[str(reply.get("notes", ""))] if reply.get("notes") else [],
    )
    decision.hosts = sorted({urlparse(e.url).netloc for e in decision.products + decision.hubs}
                            | {ctx.base_host})
    return decision


def _category_of(e: NavEntry) -> str:
    """The nearest menu label that is a category rather than a generic root."""
    for label in reversed(e.trail):
        if not re.fullmatch(r"(?i)(products?|instruments?|produkte|all products|shop)", label):
            return label
    return ""


def decide_heuristically(ctx: SiteContext, fallback_prefixes: list[str]) -> ScopeDecision:
    """No model: menu entries under a product-ish trail, minus non-instrument words."""
    decision = ScopeDecision(decided_by="heuristic", catalogue_prefixes=fallback_prefixes)
    parents = {label for e in ctx.entries for label in e.trail}
    for e in ctx.entries:
        if e.region == "footer":
            continue
        if not _PRODUCT_TRAIL.search(" ".join(e.trail) + " " + e.text):
            continue
        if _NOT_INSTRUMENT.search(e.label):
            continue
        entry = ScopeEntry(url=e.url, name=e.text, category=_category_of(e))
        (decision.hubs if e.text in parents else decision.products).append(entry)
    decision.hosts = sorted({urlparse(e.url).netloc for e in decision.products + decision.hubs}
                            | {ctx.base_host})
    decision.notes.append(
        "scope chosen by keyword heuristic over the menu — no model available; "
        "expect lower recall on vendors with unusual menu wording"
    )
    return decision


def affiliated_domains(links: list[NavEntry], domain: str) -> list[str]:
    """Other registrable domains a catalogue hub links to — a second catalogue, maybe.

    Brooks' lab automation products live on brookslabautomation.com and
    preciseflexrobots.com, linked only from brooks.com's lab-automation hub.
    """
    out: list[str] = []
    for link in links:
        reg = registrable_domain(urlparse(link.url).netloc)
        if reg != registrable_domain(domain) and reg not in out and "." in reg:
            out.append(reg)
    return out
