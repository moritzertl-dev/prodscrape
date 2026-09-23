"""Stage 0.5 — the vendor's own navigation menu, with its hierarchy.

URL structure is a weak guide to where products live. Of the six vendors this was
generalised on, three (Tecan, Formulatrix, Azenta) publish products as flat, single-segment
slugs indistinguishable from blog posts, one (Brooks) leaves them out of the sitemap
entirely, and Tecan keeps them on a different host (``lifesciences.tecan.com``) that the
corporate sitemap never mentions.

The navigation menu has none of those problems. It is the vendor's own taxonomy, written
for humans, and it is on every page:

    Products > Liquid handling & automation > Fluent®   -> /fluent-laboratory-automation-workstation

Anchor text plus ancestry is exactly the evidence a scope decision needs, at ~15 tokens
per entry — so this is what the scope step reads instead of a URL list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser, Node

NAV_CONTAINER_RE = re.compile(r"(^|[\s_-])(nav|navbar|navigation|menu|megamenu|mega-menu)", re.I)
SKIP_SCHEMES = ("#", "mailto:", "tel:", "javascript:")
ASSET_RE = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|zip|mp4)(?:\?|$)", re.I)
# Documents, with or without an extension: Tecan serves PDFs at /doc/<name>-pdf-397823.
DOCUMENT_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?)$|[-_]pdf[-_]\d+/?$", re.I)

# Language switchers, social links and account chrome: never a catalogue.
LANGUAGE_LABELS = {
    "english", "deutsch", "español", "espanol", "français", "francais", "italiano",
    "português", "portugues", "nederlands", "polski", "русский", "中文", "日本語",
    "한국어", "简体中文", "繁體中文", "türkçe", "svenska", "dansk", "suomi", "norsk",
}
# Matched as registrable domains, never substrings: "x.com" is a suffix of
# formulatrix.com, and a substring test silently dropped that vendor's entire menu.
SOCIAL_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "youtube.com",
    "instagram.com", "xing.com", "tiktok.com", "weibo.com", "wechat.com",
    "pinterest.com", "youtu.be",
}


@dataclass
class NavEntry:
    text: str
    url: str
    trail: list[str] = field(default_factory=list)   # ancestor labels, outermost first
    region: str = "nav"                               # nav | header | footer

    @property
    def label(self) -> str:
        return " > ".join([*self.trail, self.text])

    def as_dict(self) -> dict:
        return asdict(self)


# Tracking and session parameters that make one page look like many. Tecan's HubSpot
# site appends ?hsLang=en to every internal link, which doubled its candidate list.
_TRACKING_PARAM = re.compile(
    r"^(?:utm_\w+|hslang|_hs\w+|hsctatracking|gclid|fbclid|mc_cid|mc_eid|_ga|_gl|"
    r"cid|ref|referrer|source|trk|icid|sessionid|sid)$",
    re.I,
)
# Template bugs render missing values as "null"/"undefined" path segments; Tecan
# emitted 60 such links, every one a 404.
_JUNK_PATH = re.compile(r"/(?:null|undefined|none|nan)(?:/|$)|%20%20", re.I)


def canonical_url(url: str) -> str:
    """The URL without fragment and tracking parameters, for identity and fetching."""
    from urllib.parse import parse_qsl, urlencode

    parsed = urlparse(url.split("#", 1)[0])
    if parsed.query:
        kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if not _TRACKING_PARAM.match(k)]
        parsed = parsed._replace(query=urlencode(kept))
    return parsed.geturl()


def is_junk_url(url: str) -> bool:
    return bool(_JUNK_PATH.search(url))


def registrable_domain(host: str) -> str:
    """``lifesciences.tecan.com`` -> ``tecan.com``; good enough for same-vendor checks.

    Deliberately simple: two labels, or three when the second-level label is a common
    public suffix part (``co.uk``, ``com.cn``). No public-suffix list dependency.
    """
    host = host.lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "ac", "gov"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_vendor(host: str, domain: str) -> bool:
    return registrable_domain(host) == registrable_domain(domain)


def _text(node: Node) -> str:
    return " ".join(node.text(separator=" ", strip=True).split())


def _own_label(li: Node) -> str:
    """The label a menu item shows for itself, ignoring its nested submenu."""
    for child in li.iter(include_text=False):
        if child.tag in ("ul", "ol"):
            continue
        if child.tag in ("a", "span", "button", "strong", "h2", "h3", "h4", "h5", "h6", "p"):
            text = _text(child)
            if text:
                return text
        if child.tag == "div":
            # A wrapper div around the item's own link, but not the submenu panel.
            if child.css_first("ul") is None:
                text = _text(child)
                if text and len(text) <= 60:
                    return text
    return ""


def _trail(a: Node, stop: Node | None) -> list[str]:
    """Labels of the menu items enclosing this link, outermost first."""
    labels: list[str] = []
    own = _text(a)
    node = a.parent
    first_li = True
    while node is not None and node is not stop:
        if node.tag == "li":
            if first_li:
                first_li = False       # the link's own item
            else:
                label = _own_label(node)
                if label and label != own and len(label) <= 60:
                    labels.append(label)
        node = node.parent
    labels.reverse()
    # Collapse immediate repeats ("Products > Products > ...").
    out: list[str] = []
    for label in labels:
        if not out or out[-1] != label:
            out.append(label)
    return out[-3:]


def _region(node: Node) -> str:
    cur = node
    while cur is not None:
        if cur.tag == "footer":
            return "footer"
        cur = cur.parent
    return "nav"


def _is_nav_container(node: Node) -> bool:
    if node.tag in ("nav", "header", "footer"):
        return True
    if (node.attributes.get("role") or "").lower() == "navigation":
        return True
    ident = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
    return bool(NAV_CONTAINER_RE.search(ident))


def _outermost_nav_containers(tree: HTMLParser) -> list[Node]:
    """Top-level navigation containers, so nested ones are not walked twice."""
    out: list[Node] = []
    for node in tree.css("nav, header, footer, [role=navigation], [class], [id]"):
        if not _is_nav_container(node):
            continue
        parent = node.parent
        nested = False
        while parent is not None:
            if parent in out or _is_nav_container(parent):
                nested = True
                break
            parent = parent.parent
        if not nested:
            out.append(node)
    return out


# Hosts that show up in menus and content but never hold a vendor catalogue.
THIRD_PARTY_RE = re.compile(
    r"google|gstatic|cookiebot|onetrust|gmpg|w3\.org|schema\.org|hubspot|hsforms|"
    r"marketo|pardot|vimeo|wistia|doubleclick|cloudflare|jsdelivr|typekit|adobe|"
    r"bing\.|yahoo|apple\.com|microsoft|office\.com|zoom\.us|eventbrite|issuu|"
    r"slideshare|wikipedia|archive\.org|addtoany|sharethis|trustpilot|g2\.com|"
    r"crunchbase|glassdoor|indeed|kununu|workday|successfactors|greenhouse|lever\.co",
    re.I,
)

# Link texts that are calls to action, not names: "Read More about BioArc™ Duo" names
# BioArc™ Duo.
_CTA_RE = re.compile(
    r"^(?:read more|learn more|find out more|discover(?: more)?|explore|view|see|"
    r"more info(?:rmation)?|details|shop)(?: about| on| the)?\s*[:\-–]?\s*",
    re.I,
)


def clean_link_text(text: str) -> str:
    """"Read More about BioArc Duo" -> "BioArc Duo"; "Discover now" stays as it is."""
    stripped = _CTA_RE.sub("", text or "").strip()
    return stripped if len(stripped) >= 6 or " " in stripped else (text or "").strip()


def _better(new: NavEntry, held: NavEntry) -> bool:
    """Which of two links to the same page names it better.

    A tab link ("Fluent® > System Modules") carries the product's name in its trail, so
    the entry whose *text* is that trail label is the product's own. Between two links
    with the same text, the one with more ancestry wins: "Products > Liquid Handling >
    Mantis" says more than a bare "Mantis" from a teaser block.
    """
    if held.region == "footer" and new.region != "footer":
        return True
    if new.region == "footer" and held.region != "footer":
        return False
    if new.text in held.trail:
        return True
    if held.text in new.trail:
        return False
    return new.text == held.text and len(new.trail) > len(held.trail)


def extract_nav(
    html: str, page_url: str, *, domain: str | None = None, include_external: bool = False
) -> list[NavEntry]:
    """Every navigation link on a page, deduplicated by URL, with its menu ancestry.

    Links to other hosts of the same vendor are kept — that is how products on a
    subdomain are found. With ``include_external``, links to *other* domains are kept
    too, marked ``region="external"``: Brooks' menu sends "Lab Automation" to
    brookslabautomation.com, and a scope step that never sees that link cannot find the
    catalogue. Social links, language switchers and assets are always dropped.
    """
    tree = HTMLParser(html)
    domain = domain or urlparse(page_url).netloc
    entries: dict[str, NavEntry] = {}

    for container in _outermost_nav_containers(tree):
        for a in container.css("a[href]"):
            href = (a.attributes.get("href") or "").strip()
            if not href or href.startswith(SKIP_SCHEMES):
                continue
            url = canonical_url(urljoin(page_url, href))
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or is_junk_url(url):
                continue
            host = parsed.netloc.lower()
            if registrable_domain(host) in SOCIAL_DOMAINS or THIRD_PARTY_RE.search(host):
                continue
            external = not same_vendor(host, domain)
            if external and not include_external:
                continue
            if ASSET_RE.search(parsed.path):
                continue
            text = _text(a) or (a.attributes.get("title") or "").strip() \
                or (a.attributes.get("aria-label") or "").strip()
            if not text or len(text) > 80 or text.lower() in LANGUAGE_LABELS:
                continue
            region = "external" if external else (
                "footer" if container.tag == "footer" else _region(container))
            # Mega-menus link one product page once per tab ("Overview", "Software",
            # "Literature" ... all -> /fluent-...?tab=n). Keyed on host+path, the
            # product survives once, under the entry nearest the menu root — which is
            # the one carrying its real name rather than a tab label.
            key = f"{host}{parsed.path.rstrip('/')}"
            entry = NavEntry(text=text, url=url, trail=_trail(a, container), region=region)
            held = entries.get(key)
            if held is None or _better(entry, held):
                entries[key] = entry
    return list(entries.values())


# Listing pages continue on "?page=2", "?p=3", "/page/2/". Such links are kept apart
# from the bare listing URL and followed as more of the same hub.
PAGINATION_RE = re.compile(r"(?:^|&)(?:page|p|pg|paged|start|offset)=\d+", re.I)
PAGINATION_PATH_RE = re.compile(r"/page/\d+/?$", re.I)


def is_pagination(url: str) -> bool:
    parsed = urlparse(url)
    return bool(PAGINATION_RE.search(parsed.query) or PAGINATION_PATH_RE.search(parsed.path))


CHROME_TAGS = ("nav", "header", "footer", "aside", "script", "style", "noscript", "form")
STRUCTURAL_TAGS = ("html", "body", "main", "article")
CHROME_CLASS_RE = re.compile(
    r"(^|[\s_-])(nav|navbar|menu|megamenu|header|footer|breadcrumbs?|cookie|consent|"
    r"banner|sidebar|subnav|topbar|skip-link|social|share)([\s_-]|$)",
    re.I,
)


def content_links(
    html: str, page_url: str, *, domain: str | None = None, any_domain: bool = False
) -> list[NavEntry]:
    """Links in a page's *content* — what a category page lists, minus the chrome.

    The same chrome-stripping guard as text extraction applies: an element carrying a
    large share of the page is never treated as chrome whatever its class says, because
    some themes wrap the whole article in ``class="...header..."``.
    """
    tree = HTMLParser(html)
    domain = domain or urlparse(page_url).netloc
    for tag in CHROME_TAGS:
        for node in tree.css(tag):
            node.decompose()
    # Measured *after* the tag strip. Azenta's <body> carries class="mega-menu-header";
    # against the pre-strip total, the body itself fell under the chrome threshold once
    # its 60 KB mega-menu was gone, and was removed — taking every link with it.
    body = tree.body
    total = len(body.text(separator=" ", strip=True)) if body else 0
    for node in tree.css("[class], [id]"):
        if node.tag in STRUCTURAL_TAGS:
            continue
        ident = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if CHROME_CLASS_RE.search(ident) and len(
            node.text(separator=" ", strip=True)
        ) < total * 0.4:
            node.decompose()

    out: dict[str, NavEntry] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(SKIP_SCHEMES):
            continue
        url = canonical_url(urljoin(page_url, href))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or ASSET_RE.search(parsed.path):
            continue
        if is_junk_url(url) or DOCUMENT_RE.search(parsed.path):
            continue
        host = parsed.netloc.lower()
        if registrable_domain(host) in SOCIAL_DOMAINS or THIRD_PARTY_RE.search(host):
            continue
        if not any_domain and not same_vendor(host, domain):
            continue
        text = clean_link_text(_text(a) or (a.attributes.get("title") or "").strip()
                               or (a.attributes.get("aria-label") or "").strip())
        key = f"{host}{parsed.path.rstrip('/')}" + (f"?{parsed.query}" if PAGINATION_RE.search(
            parsed.query) else "")
        # Card grids link each product twice — image and title. Keep the longer,
        # human-readable text; "Learn more" loses to "Fluent® Automation Workstation".
        if key in out:
            if len(text) > len(out[key].text) and len(text) <= 120:
                out[key].text = text
            continue
        out[key] = NavEntry(text=text[:120], url=url, region="body")
    return list(out.values())


def render_nav(entries: list[NavEntry], *, base_host: str = "", limit: int = 400) -> str:
    """Compact, numbered, one line per entry — what the scope step reads."""
    lines = []
    for i, e in enumerate(entries[:limit]):
        parsed = urlparse(e.url)
        host = "" if parsed.netloc == base_host else parsed.netloc
        tag = " (footer)" if e.region == "footer" else ""
        lines.append(f"[{i}] {e.label}{tag} -> {host}{parsed.path or '/'}")
    return "\n".join(lines)
