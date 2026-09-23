"""Stage 1b — from a scope decision to a candidate list, by a focused crawl.

A sitemap is one witness to what a vendor sells; category pages are another, and often
the better one. Brooks' sitemap holds 45 URLs and no product page at all; Tecan's
corporate sitemap never mentions the host its products live on. So candidates are the
union of three sources, each tagged with how it was found:

``nav``      a page the scope step named as an instrument, from the vendor's menu
``hub``      a content link on a category page (the chrome is stripped first)
``sitemap``  a leaf URL under a catalogue branch the scope step named

A hub's links are followed one level; a candidate that turns out to be a listing page
itself (many product-like links, no spec heading) is expanded in turn, up to
``max_depth``. The whole crawl is bounded by ``max_pages``.

Two deterministic filters keep the crawl on the catalogue:

* editorial paths (blog, news, events, careers, literature ...) are never followed;
* a link the vendor's *menu* lists but the scope step did not choose is skipped — the
  model already looked at "Consumables" in the menu and said no, so a hub linking to it
  does not reopen the question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

from .inventory import leaf_urls
from .navigation import (
    THIRD_PARTY_RE, NavEntry, content_links, is_pagination, registrable_domain,
)
from .scope import ScopeDecision

EDITORIAL_PATH = re.compile(
    r"/(?:blogs?|news(?:room)?|press|events?|webinars?|careers?|jobs|about(?:-us)?|"
    r"contact(?:-us)?|legal|privacy|imprint|impressum|cookies?(?:-policy)?|terms|login|"
    r"account|cart|checkout|search|request-(?:a-)?quote|get-a-quote|literature|downloads?|"
    r"resources|case-stud(?:y|ies)|podcasts?|videos?|investors?|sustainability|"
    r"(?:[a-z-]*-)?(?:forms?|registration|survey|promotions?|offers?)|tag|author|"
    r"feed|wp-json|sitemap|locations?|distributors?|support|training|academy|"
    r"journal|[a-z-]*citations?|doclist[a-z-]*|docs?|publications?|topics?|insights?|"
    r"stories|story|app(?:lication)?-notes?|white-?papers?|e-?books?|brochures?|"
    r"webcasts?|library|glossary|faqs?|partners?|shop|store|ecommerce)(?:/|$|[-_])",
    re.I,
)
THIRD_PARTY = THIRD_PARTY_RE


@dataclass
class Candidate:
    url: str
    name: str = ""
    category: str = ""
    via: str = "sitemap"          # nav | hub | sitemap | recipe
    parent: str = ""              # the hub it was found on
    depth: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


PAGE_PATH = re.compile(r"/page/\d+/?$", re.I)


def _key(url: str) -> str:
    p = urlparse(url)
    query = f"?{p.query}" if is_pagination(url) and p.query else ""
    return f"{p.netloc.lower()}{p.path.rstrip('/')}{query}"


def _prefix_match(url: str, prefix: str, base_host: str) -> bool:
    p = urlparse(url)
    if prefix.startswith("/"):
        return p.netloc == base_host and p.path.startswith(prefix.rstrip("*"))
    host, _, path = prefix.partition("/")
    return p.netloc == host and p.path.startswith("/" + path.rstrip("*"))


def sitemap_candidates(
    urls: list[str], decision: ScopeDecision, base_host: str
) -> list[Candidate]:
    """Leaf sitemap URLs under the chosen catalogue branches."""
    if not decision.catalogue_prefixes:
        return []
    leaves = leaf_urls(urls)
    out = []
    for u in urls:
        if u not in leaves:
            continue
        if not any(_prefix_match(u, p, base_host) for p in decision.catalogue_prefixes):
            continue
        if any(_prefix_match(u, p, base_host) for p in decision.exclude_prefixes):
            continue
        if EDITORIAL_PATH.search(urlparse(u).path):
            continue
        out.append(Candidate(url=u, via="sitemap"))
    return out


class Frontier:
    """Ordered, de-duplicated work list for the focused crawl."""

    def __init__(self, decision: ScopeDecision, menu: list[NavEntry], domain: str,
                 base_host: str):
        self.decision = decision
        self.domain = domain
        self.base_host = base_host
        self.items: list[tuple[str, Candidate]] = []     # (kind, candidate)
        self.seen: set[str] = set()
        chosen = {_key(e.url) for e in decision.products + decision.hubs}
        # Menu entries the scope step saw and did not choose.
        self.rejected = {_key(e.url) for e in menu} - chosen
        self.affiliated_links: list[NavEntry] = []
        # Links harvested from hubs wait here for triage before they cost a fetch.
        self.pending: list[Candidate] = []

    def push(self, kind: str, cand: Candidate) -> bool:
        k = _key(cand.url)
        if not k or k in self.seen:
            return False
        self.seen.add(k)
        self.items.append((kind, cand))
        return True

    def seed(self, sitemap: list[Candidate]) -> None:
        for e in self.decision.products:
            self.push("page", Candidate(url=e.url, name=e.name, category=e.category,
                                        via="nav"))
        for e in self.decision.hubs:
            self.push("hub", Candidate(url=e.url, name=e.name, category=e.category or e.name,
                                       via="nav"))
        for c in sitemap:
            self.push("page", c)

    def expand(self, html: str, hub: Candidate, allowed_hosts: set[str]) -> int:
        """Queue the product-like content links of a hub page. Returns how many."""
        added = 0
        allowed_domains = {registrable_domain(h) for h in allowed_hosts}
        for link in content_links(html, hub.url, domain=self.domain, any_domain=True):
            parsed = urlparse(link.url)
            host = parsed.netloc.lower()
            reg = registrable_domain(host)
            if reg != registrable_domain(self.domain) and reg not in allowed_domains:
                if hub.via == "nav":
                    self.affiliated_links.append(link)
                continue
            if is_pagination(link.url) and _key(link.url.split("?")[0]) == _key(
                    hub.url.split("?")[0]) or PAGE_PATH.search(parsed.path) and \
                    parsed.path.startswith(urlparse(hub.url).path.rstrip("/")):
                # More of the same listing: follow it as the same hub.
                if self.push("hub", Candidate(url=link.url, name=hub.name,
                                              category=hub.category, via=hub.via,
                                              parent=hub.url, depth=hub.depth)):
                    added += 1
                continue
            if EDITORIAL_PATH.search(parsed.path) or parsed.path.strip("/") == "":
                continue
            if _key(link.url) in self.rejected:
                continue
            if any(_prefix_match(link.url, p, self.base_host)
                   for p in self.decision.exclude_prefixes):
                continue
            category = hub.name if hub.via == "nav" else (hub.category or hub.name)
            k = _key(link.url)
            if k in self.seen:
                continue
            self.seen.add(k)
            self.pending.append(Candidate(url=link.url, name=link.text, category=category,
                                          via="hub", parent=hub.url, depth=hub.depth + 1))
            added += 1
        return added

    def release(self, keep) -> int:
        """Move triaged links into the work list; ``keep(cands) -> list[bool]``."""
        if not self.pending:
            return 0
        batch, self.pending = self.pending, []
        decisions = keep(batch)
        n = 0
        for cand, ok in zip(batch, decisions):
            if ok:
                self.items.append(("page", cand))
                n += 1
        return n


def product_link_count(html: str, url: str, domain: str) -> int:
    """How many non-editorial same-vendor content links a page carries.

    Used to recognise a listing page reached as a candidate: Tecan's
    "/microplate-readers" and Beckman's "/centrifuges/ultracentrifuges" are both
    category pages sitting in exactly the places product pages usually sit.
    """
    n = 0
    for link in content_links(html, url, domain=domain):
        path = urlparse(link.url).path
        if path.strip("/") and not EDITORIAL_PATH.search(path) and _key(link.url) != _key(url):
            n += 1
    return n
