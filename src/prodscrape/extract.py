"""Stage 3 — turn a classified instrument page into device records.

Deterministic throughout: the spec table is located structurally (by the heading that
precedes it), parsed with orientation detection, split into devices by the functional
hardware rule in ``devices.py``, and every value keeps its raw text.

No model call happens here. Claude's role in this stage is mapping *unseen attribute
labels* onto schema keys, which is cached in the recipe — not reading pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from .devices import group_devices
from .normalize import SpecValue, attribute_key, parse_spec_value
from .recipes import Recipe
from .signals import SPEC_HEADINGS, looks_like_date
from .tables import _row_cells, detect_orientation, to_spec_table

# Words that mark a table as something other than specifications, in the languages the
# tool supports. Matched against header cells individually, so this generalises across
# vendors instead of encoding one vendor's exact header tuples.
# Words that alone prove a table is not a specification table. These never appear as a
# spec attribute name.
STRONG_NON_SPEC_WORDS = {
    "language", "sprache", "langue", "idioma", "lingua",
    "download", "downloads", "brochure", "brochures", "flyer",
    "filesize", "cookie", "consent", "provider", "expiry",
}

# Words that merely *suggest* it. A real attribute can legitimately be called "Sample
# size", "File format" or "Status", and in a variant-major table row 0 holds attribute
# names — so a single weak word here once rejected 39 genuine Analytik Jena spec tables.
# Only a narrow table carrying two or more of them is treated as non-spec.
WEAK_NON_SPEC_WORDS = {
    "info", "size", "format", "status", "file", "datasheet",
    "required", "included", "optional", "accessory", "accessories",
    "price", "preis", "prix", "precio", "availability", "stock",
    "description", "title", "order",
}
WEAK_WORD_MAX_COLUMNS = 4

# Two-letter language codes in a column mean a downloads table, whatever it is titled.
LANGUAGE_CODE_RE = re.compile(r"^(?:de|en|fr|es|it|nl|pt|pl|cs|ru|zh|ja|ko)$", re.I)
FILE_SIZE_RE = re.compile(r"^\s*(?:pdf|docx?|xlsx?|zip)[, ]+.*\d+\s*[kmg]b\s*$", re.I)

# Connectivity is first-class for the landscape/integration use case, so interfaces are
# pulled from the whole page rather than only the spec table.
INTERFACE_PATTERNS = {
    "RS-232": r"\bRS[-\s]?232\b",
    "RS-485": r"\bRS[-\s]?485\b",
    "USB": r"\bUSB\b",
    "Ethernet": r"\bEthernet\b|\bRJ[-\s]?45\b|\bTCP/IP\b|\bLAN\b",
    "CAN": r"\bCAN[-\s]?bus\b",
    "GPIB": r"\bGPIB\b|\bIEEE[-\s]?488\b",
    "Bluetooth": r"\bBluetooth\b",
    "WLAN": r"\bWLAN\b|\bWi[-\s]?Fi\b",
    "SiLA": r"\bSiLA[-\s]?2?\b",
    "OPC-UA": r"\bOPC[-\s]?UA\b",
    "Modbus": r"\bModbus\b",
    "Serial": r"\bserial\s+(?:port|interface|connection)\b",
    # "in" and "out" alone match ordinary prose — "Digital in PDF format" on a
    # qualification-documents line was being read as a digital I/O interface on 87 of one
    # vendor's 90 pages. Require the full word, or an explicit I/O form.
    "Analog I/O": r"\banalog(?:ue)?\s+(?:inputs?|outputs?|i/o|interface)\b",
    "Digital I/O": r"\bdigital\s+(?:inputs?|outputs?|i/o|interface)\b",
}

# A PDF link, with or without the extension (Tecan: /doc/spark-datasheet-pdf-397823).
_PDF_RE = re.compile(r"\.pdf(\?|$)|[-_]pdf[-_]\d+/?(\?|$)", re.I)


@dataclass
class DeviceRecord:
    """One row of the final table: a single device."""

    product_id: str
    manufacturer: str
    name: str
    url: str
    category: str = ""
    description: str = ""
    image_url: str = ""
    datasheet_urls: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    specs: dict[str, SpecValue] = field(default_factory=dict)
    source_variants: list[str] = field(default_factory=list)
    regional_variants: list[dict] = field(default_factory=list)
    merge_reason: str = ""
    spec_source: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "manufacturer": self.manufacturer,
            "name": self.name,
            "url": self.url,
            "category": self.category,
            "description": self.description,
            "image_url": self.image_url,
            "datasheet_urls": self.datasheet_urls,
            "interfaces": self.interfaces,
            "specs": {k: v.as_dict() for k, v in self.specs.items()},
            "source_variants": self.source_variants,
            "regional_variants": self.regional_variants,
            "merge_reason": self.merge_reason,
            "spec_source": self.spec_source,
            "warnings": self.warnings,
        }


def _looks_like_non_spec(grid: list[list[str]]) -> bool:
    """Whether a table is a downloads / order / cookie list rather than specifications.

    Structural, not vendor-specific: a non-spec header word, a column of language codes,
    or a column of file-size strings each settle it on any site in any of the supported
    languages.
    """
    header_words = {
        word
        for cell in grid[0]
        for word in re.split(r"[^a-zä-ÿ]+", cell.strip().lower())
        if word
    }
    if header_words & STRONG_NON_SPEC_WORDS:
        return True
    if (
        len(grid[0]) <= WEAK_WORD_MAX_COLUMNS
        and len(header_words & WEAK_NON_SPEC_WORDS) >= 2
    ):
        return True

    for col in range(len(grid[0])):
        cells = [r[col].strip() for r in grid[1:] if col < len(r) and r[col].strip()]
        if len(cells) < 2:
            continue
        if sum(1 for c in cells if LANGUAGE_CODE_RE.match(c)) >= len(cells) * 0.6:
            return True
        if sum(1 for c in cells if FILE_SIZE_RE.match(c)) >= len(cells) * 0.6:
            return True
    return False


def tables_with_headings(html: str) -> list[tuple[str, list[list[str]]]]:
    """Every table paired with the nearest heading above it, in document order.

    Document order matters and ``css()`` does not preserve it across a selector group,
    so the tree is walked directly.
    """
    tree = HTMLParser(html)
    out: list[tuple[str, list[list[str]]]] = []
    current = ""
    for node in tree.root.traverse(include_text=False):
        if node.tag in ("h1", "h2", "h3", "h4"):
            text = " ".join(node.text(separator=" ", strip=True).split())
            if text:
                current = text
        elif node.tag == "table":
            grid = [cells for row in node.css("tr") if (cells := _row_cells(row))]
            if grid:
                width = max(len(r) for r in grid)
                out.append((current, [r + [""] * (width - len(r)) for r in grid]))
        elif node.tag == "dl":
            grid = _definition_list_grid(node)
            if grid:
                out.append((current, grid))
    return out


def _definition_list_grid(node) -> list[list[str]]:
    """A ``<dl>`` rendered as a two-column grid.

    Plenty of vendors mark specifications up as definition lists rather than tables, and
    reading only ``<table>`` meant those pages produced no specs at all. The synthetic
    blank header row makes orientation detection read it as attribute-major, which is
    what a definition list always is.
    """
    pairs: list[list[str]] = []
    label: str | None = None
    for child in node.iter(include_text=False):
        if child.tag == "dt":
            label = _cell_text_of(child)
        elif child.tag == "dd" and label is not None:
            value = _cell_text_of(child)
            if label and value:
                pairs.append([label, value])
            label = None
    return [["", ""], *pairs] if len(pairs) >= 2 else []


def _cell_text_of(node) -> str:
    return " ".join(node.text(separator=" ", strip=True).split())


# A cell that is nothing but a catalogue code. Generic across vendors, with dates
# excluded — see signals.looks_like_date.
ORDER_NUMBER_CELL_RE = re.compile(
    r"^\s*(?=[A-Z0-9]*\d)[A-Z0-9]{2,}[-./][A-Z0-9]{2,}[-./][A-Z0-9]{1,}\s*$", re.I
)

# A spec table describes a handful of variants. A table listing dozens of "entities" is an
# accessory or consumables catalogue, not a specification.
MAX_VARIANTS_PER_TABLE = 12
MIN_ATTRIBUTES_PER_TABLE = 3


def _is_order_list(grid: list[list[str]]) -> bool:
    """Whether a column is dominated by order numbers — an accessory list, not specs."""
    for col in range(min(2, len(grid[0]))):
        cells = [r[col] for r in grid[1:] if r[col].strip()]
        if not cells:
            continue
        hits = sum(
            1 for c in cells
            if ORDER_NUMBER_CELL_RE.match(c) and not looks_like_date(c)
        )
        if hits >= max(2, len(cells) * 0.5):
            return True
    return False


def find_spec_table(
    html: str, recipe: Recipe | None = None
) -> tuple[list[list[str]] | None, str]:
    """Locate the specification table. Returns ``(grid, provenance)``.

    A table must be *validated* as a spec table, not merely be the biggest one on the
    page. Falling back to the largest table pulled in analytik-jena's 111-row accessory
    catalogues and produced 425 phantom devices — a wrong table yields confident,
    plausible-looking nonsense, so no table is better than the wrong one.
    """
    spec_terms = tuple(SPEC_HEADINGS) + tuple(
        h.lower() for h in (recipe.spec_headings if recipe else ())
    )
    orientation = recipe.spec_table_orientation if recipe else None

    scored: list[tuple[int, int, str, list[list[str]]]] = []
    for heading, grid in tables_with_headings(html):
        heading_match = any(term in heading.lower() for term in spec_terms)
        # A table under "Resources" or "Downloads" is not a spec table whatever its
        # shape: Formulatrix's NT8 page produced two phantom devices, "Wet Dispense"
        # and "Dry Dispense", from its resources grid.
        if not heading_match and NON_SPEC_HEADING_RE.search(heading):
            continue

        # Size bars are relaxed under a spec heading and strict without one. A page
        # describing a single device has a two-row spec table (header + one row), and
        # a flat "> 2 rows" rule rejected real instruments outright — multi X 2500's
        # Technical Data table is 2x13. Without a heading to vouch for it, a table that
        # small is far more likely to be a stray layout table, so the bar stays high.
        min_rows = 2 if heading_match else 3
        min_attributes = 1 if heading_match else MIN_ATTRIBUTES_PER_TABLE

        if len(grid) < min_rows or len(grid[0]) < 2:
            continue
        if _looks_like_non_spec(grid) or _is_order_list(grid):
            continue
        table = to_spec_table(grid, orientation)
        entities = [e for e in table.entities if e.strip()]
        attributes = [a for a in table.attributes if a.strip()]
        if not (1 <= len(entities) <= MAX_VARIANTS_PER_TABLE):
            continue
        if len(attributes) < min_attributes:
            continue
        scored.append((1 if heading_match else 0, len(attributes), heading, grid))

    if not scored:
        return None, "none"

    heading_match, _, heading, grid = max(scored, key=lambda s: (s[0], s[1]))
    label = "heading" if heading_match else "validated-table"
    return grid, f"{label}:{heading or 'no heading'}"


NON_SPEC_HEADING_RE = re.compile(
    r"\b(resources?|downloads?|literature|documents?|brochures?|publications?|"
    r"citations?|references|webinars?|videos?|application notes?|accessor(?:y|ies)|"
    r"consumables?|ordering|related|you may also|news|events?|faqs?|support)\b",
    re.I,
)

# --------------------------------------------------------------------------- sections

SECTION_TEXT_TAGS = ("p", "li", "dd", "dt", "td")
_LABEL_VALUE_RE = re.compile(r"^\s*([^:：]{2,60}?)\s*[:：]\s*(.+?)\s*$")
MAX_SECTION_PAIRS = 120


_TOGGLE_RE = re.compile(r"accordion|toggle|collaps|tab(?:s|-title|-label|-button|__)|"
                        r"elementor-tab|panel-title|expand", re.I)
PSEUDO_SECTION_LEVEL = 2
PSEUDO_GROUP_LEVEL = 7


def _heading_level(node) -> int | None:
    """Heading level, including the two things vendors use *instead* of headings.

    * Accordion and tab labels start sections: Formulatrix's "Specifications and
      Requirements" is a ``<span>`` inside a ``role="button"`` accordion toggle.
    * A paragraph that is nothing but bold text is a sub-heading
      (``<p><strong>Electrical Specifications</strong></p>``).
    """
    if node.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return int(node.tag[1])
    if node.tag in ("button", "a", "div", "span", "li", "summary", "dt"):
        role = (node.attributes.get("role") or "").lower()
        ident = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if node.tag == "summary" or role in ("button", "tab") or _TOGGLE_RE.search(ident):
            text = node.text(separator=" ", strip=True)
            if text and len(text) <= 70 and node.css_first("p, ul, table, div div") is None:
                return PSEUDO_SECTION_LEVEL
    if node.tag == "p":
        bold = node.css_first("strong, b")
        text = " ".join(node.text(separator=" ", strip=True).split())
        if bold is not None and text and len(text) <= 70 and \
                " ".join(bold.text(separator=" ", strip=True).split()) == text:
            return PSEUDO_GROUP_LEVEL
    return None


def spec_section_pairs(
    html: str, recipe: Recipe | None = None, *, whole: bool = False
) -> tuple[dict[str, str], str]:
    """Specifications written as text under a spec heading, not as a table.

    A large share of vendors do this — Formulatrix's Mantis page lays out
    "Specifications and Requirements" as sub-headed blocks of ``Label: value`` lines
    ("Weight: 5.2 kg") and short lists ("4-40 °C ambient temperature"). Tables-only
    extraction returned nothing for every such page.

    Reading rules, all structural:

    * the section starts at a heading matching the spec vocabulary and ends at the next
      heading of the same or a higher level that does not;
    * a ``Label: value`` line is one spec; a repeated label is qualified by its
      sub-heading ("Height (Mantis with LC3 Dimensions)") instead of overwriting;
    * free lines under a sub-heading become one spec named after the sub-heading, joined
      with "; " — prose stays prose, but nothing is dropped.

    Returns ``(pairs, heading)``; empty when there is no such section.
    """
    spec_terms = tuple(SPEC_HEADINGS) + tuple(
        h.lower() for h in (recipe.spec_headings if recipe else ())
    )
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript", "nav", "footer"):
        for node in tree.css(tag):
            node.decompose()

    best: tuple[dict[str, str], str] = ({}, "")
    # ``whole``: the HTML is already one product's section (see ``product_sections``),
    # so reading starts at once and only Label: value lines count — free prose from a
    # marketing block is not a specification.
    active_level: int | None = 0 if whole else None
    section_heading = ""
    group = ""
    pairs: dict[str, str] = {}
    free: dict[str, list[str]] = {}
    seen_text: set[int] = set()
    active_pseudo = False

    def close() -> None:
        nonlocal best
        merged = dict(pairs)
        for g, lines in free.items():
            if lines:
                key = g or section_heading
                text = "; ".join(lines)[:400]
                merged.setdefault(key, text)
        if len(merged) > len(best[0]):
            best = (merged, section_heading)

    for node in tree.root.traverse(include_text=False):
        level = _heading_level(node)
        if level is not None:
            text = " ".join(node.text(separator=" ", strip=True).split())
            if not text:
                continue
            is_spec = any(t in text.lower() for t in spec_terms)
            pseudo = node.tag not in ("h1", "h2", "h3", "h4", "h5", "h6")
            if active_level is None:
                if is_spec:
                    active_level, section_heading, group = level, text, ""
                    active_pseudo = pseudo
                    pairs, free = {}, {}
                continue
            # A tab strip inside a real "Technical data" section ("Dimensions",
            # "Electrical") groups specs; it only ends a section a toggle opened.
            closes = level <= active_level and not is_spec and (
                not pseudo or active_pseudo
            )
            if closes and not whole:
                close()
                active_level = None
                continue
            group = text
            continue
        if active_level is None or node.tag not in SECTION_TEXT_TAGS:
            continue
        # Nested text tags (li > p) would be read twice.
        if any(a.mem_id in seen_text for a in _ancestors(node)):
            continue
        seen_text.add(node.mem_id)
        for line in node.text(separator="\n", strip=True).split("\n"):
            line = " ".join(line.split())
            if not line or len(line) > 300:
                continue
            m = _LABEL_VALUE_RE.match(line)
            if m and not m.group(1).lower().startswith(("http", "www")):
                label, value = m.group(1).strip(), m.group(2).strip()
                if whole and not _HAS_DIGIT.search(value):
                    continue            # a marketing "Title: tagline", not a spec
                if label in pairs and pairs[label] != value:
                    label = f"{label} ({group})" if group else f"{label} #{len(pairs)}"
                pairs.setdefault(label, value)
            elif not whole:
                free.setdefault(group, []).append(line)
        if len(pairs) >= MAX_SECTION_PAIRS:
            break
    if active_level is not None:
        close()
    return best


def _ancestors(node):
    cur = node.parent
    while cur is not None:
        yield cur
        cur = cur.parent


# --------------------------------------------------------------------------- sections

_NOT_PRODUCT_ANCHOR = re.compile(
    r"^(?:(?:back )?to (?:the )?top|top|back|up|home|contact(?: us)?|overview|features?|"
    r"benefits?|downloads?|videos?|resources?|faqs?|specs?|specifications?|"
    r"applications?|more|menu|content|main|skip to (?:main )?content|details|versions?|"
    r"part numbers?|ordering(?: information)?|order info|accessories|documents?|"
    r"documentation|support|reviews?|description|related products?|literature|"
    r"request (?:a )?quote.*|contact sales|get a quote|compare|models?)$",
    re.I,
)


def _looks_like_family_member(name: str, names: list[str]) -> bool:
    if len(name.split()) > 8 or name.rstrip().endswith("?"):
        return False                     # FAQ questions are in-page anchors too
    if _HAS_DIGIT.search(name) and _HAS_LETTER.search(name):
        return True
    mine = {t for t in _tokens(name) if len(t) >= 4}
    return any(mine & {t for t in _tokens(o) if len(t) >= 4} for o in names if o != name)


def product_sections(html: str, url: str) -> list[tuple[str, str]]:
    """``[(name, section_html)]`` for a page that presents several products in-page.

    PreciseFlex's "Recommended Products" page is five robots, each a section opened by
    an in-page link (``#preciseflex400_labproducts``) with no page of its own. The
    pattern is structural and common: two or more same-page fragment links whose
    targets exist, with link texts that are names rather than "Top" or "Downloads".

    The page's HTML is cut at each target's enclosing heading, so everything from one
    product's heading to the next product's heading is that product's section.
    """
    tree = HTMLParser(html)
    base = url.split("#", 1)[0].rstrip("/")
    targets: dict[str, str] = {}
    for a in tree.css("a[href*='#']"):
        href = (a.attributes.get("href") or "").strip()
        page, _, frag = href.partition("#")
        if not frag or (page and urljoin(url, page).split("#")[0].rstrip("/") != base):
            continue
        name = clean_label(" ".join(a.text(separator=" ", strip=True).split()))
        if len(name) < 2 or len(name) > 80 or _NOT_PRODUCT_ANCHOR.match(name):
            continue
        targets.setdefault(frag, name)
    if len(targets) < 2:
        return []
    # In-page tabs ("Details", "Part Numbers", "Versions") use the same mechanism as
    # product sections. Products in one section list look like a family: they share a
    # name ("PreciseFlex 400", "PreciseFlex c5") or carry model numbers.
    names = list(targets.values())
    family = [n for n in names if _looks_like_family_member(n, names)]
    if len(family) < 2 or len(family) < 0.6 * len(names):
        return []
    targets = {f: n for f, n in targets.items() if n in family}

    cuts: list[tuple[int, str]] = []
    for frag, name in targets.items():
        pos = html.find(f'id="{frag}"')
        if pos < 0:
            pos = html.find(f"id='{frag}'")
        if pos < 0:
            continue
        # Start at the heading that holds the marker, if it sits inside one.
        head = max(html.rfind("<h", 0, pos), 0)
        start = head if head and pos - head < 400 else html.rfind("<", 0, pos)
        cuts.append((start, name))
    cuts.sort()
    if len(cuts) < 2:
        return []
    end = html.find("<footer")
    end = end if end > cuts[-1][0] else len(html)
    out = []
    for i, (start, name) in enumerate(cuts):
        stop = cuts[i + 1][0] if i + 1 < len(cuts) else end
        out.append((name, html[start:stop]))
    return out


def extract_sections(
    url: str,
    html: str,
    sections: list[tuple[str, str]],
    *,
    manufacturer: str,
    recipe: Recipe | None = None,
    category: str | None = None,
) -> list[DeviceRecord]:
    """One device per in-page product section, specs read from that section only."""
    tree = HTMLParser(html)
    image = _meta(tree, 'meta[property="og:image"]')
    category = clean_label(category or category_from_url(url, recipe))
    records = []
    for name, chunk in sections:
        grid, provenance = find_spec_table(chunk, recipe)
        specs: dict[str, SpecValue] = {}
        if grid is not None:
            table = to_spec_table(grid, recipe.spec_table_orientation if recipe else None)
            if len(table.entities) == 1:
                specs = _specs_from(next(iter(table.records.values())))
        if not specs:
            pairs, _ = spec_section_pairs(chunk, recipe, whole=True)
            specs, provenance = _specs_from(pairs), "section:in-page"
        chunk_tree = HTMLParser(chunk)
        text = " ".join(chunk_tree.text(separator=" ", strip=True).split())
        first_p = chunk_tree.css_first("p")
        records.append(DeviceRecord(
            product_id=product_id(manufacturer, url, name),
            manufacturer=manufacturer,
            name=name,
            url=f"{url.split('#')[0]}",
            category=category,
            description=(first_p.text(strip=True) if first_p else text)[:400],
            image_url=urljoin(url, image) if image else "",
            datasheet_urls=[urljoin(url, a.attributes.get("href") or "")
                            for a in chunk_tree.css("a[href]")
                            if _PDF_RE.search(a.attributes.get("href") or "")][:10],
            interfaces=sorted(n for n, pat in INTERFACE_PATTERNS.items()
                              if re.search(pat, text, re.I)),
            specs=specs,
            spec_source=f"in-page-section:{provenance}",
        ))
    return records


# --------------------------------------------------------------------------- names

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2 and not t.isdigit()}


# "10 µl", "1.5 mL", "230 V": a quantity, not a model name.
_QUANTITY_RE = re.compile(
    r"^[<>≤≥~±]?\s*\d+(?:[.,]\d+)?\s*(?:[-–]\s*\d+(?:[.,]\d+)?\s*)?"
    r"(?:[µμu]?[lL]|m[lL]|nl|mm|cm|m|µm|nm|kg|g|mg|V|kV|Hz|kHz|W|kW|A|rpm|x\s*g|°C|%|bar|"
    r"psi|min|s|h|well|wells|plates?|samples?|tubes?|channels?)\.?$",
    re.I,
)
_HAS_LETTER = re.compile(r"[A-Za-z]")
_STOPWORDS = {"and", "the", "for", "with", "of", "to", "in", "on", "your", "our", "a",
              "an", "und", "der", "die", "das", "von", "mit", "for", "by", "at"}
# Calls to action and generic labels: never a device name.
_CTA_NAME = re.compile(r"(?i)(?:learn|read|find out|discover|see|view|explore) (?:more|how|now)|"
                       r"explore|overview|details|"
                       r"products?|home|click here|more info(?:rmation)?")
_HAS_DIGIT = re.compile(r"\d")


def plausible_device_names(names: list[str], page_name: str, hint: str = "") -> bool:
    """Whether a table's entity axis names devices at all.

    A comparison table read along the wrong axis names *attributes* as devices
    ("Lens/Objective Options", "Working Distance"); a sample-size table names numbers
    ("50", "15", "1.5"). Real variant names share a word with the product
    ("ROCK IMAGER 1000" on the Rock Imager page) or look like a model code ("B 28",
    "M Nano+"). Most of the entities must pass.
    """
    names = [n for n in names if n.strip()]
    if not names:
        return False
    family = _tokens(page_name) | _tokens(hint)
    # "qTOWER iris 384" on the "qTOWERiris Series" page: compare against the family
    # name with its spaces removed as well.
    glued = re.sub(r"[^a-z0-9]", "", f"{page_name} {hint}".lower())
    ok = 0
    for n in names:
        if re.fullmatch(r"column_\d+", n.strip()):
            continue                        # a placeholder: the table named no variants
        toks = _tokens(n)
        if toks & family or any(len(t) >= 4 and t in glued for t in toks):
            ok += 1
        elif (_HAS_LETTER.search(n) and _HAS_DIGIT.search(n) and len(n) <= 100
              and not _QUANTITY_RE.match(n.strip())):
            ok += 1                         # a model designation: "AFU 3", "LS-T", "B 28"
    return ok >= max(1, (len(names) + 1) // 2)


# Chrome that surrounds the content on every page. Left in, a "USB-C accessories" entry
# in a global nav gave every product on the site a USB interface it does not have.
CHROME_TAGS = ("nav", "header", "footer", "aside", "script", "style", "noscript")
CHROME_CLASS_RE = re.compile(
    r"\b(nav|menu|header|footer|breadcrumb|cookie|consent|banner|sidebar|"
    r"megamenu|subnav|topbar|skip-link)\b",
    re.I,
)


def main_content_text(html: str, *, min_retained: float = 0.3) -> str:
    """Page text with navigation, headers, footers and cookie banners removed.

    Interfaces are read from the whole page rather than only the spec table, so the
    surrounding chrome has to go first — otherwise a global nav gives every product on
    the site the same interfaces.

    Stripping by class name is unavoidably a guess: a vendor that wraps its entire page
    in a ``<div class="...header...">`` loses everything, which is exactly what happened
    on one site — 11,907 characters of page became 33. So the result is checked, and if
    less than ``min_retained`` of the text survives the strip is treated as having gone
    wrong and the full body is used. Over-reporting an interface is a far smaller error
    than reporting none.
    """
    full_tree = HTMLParser(html)
    full = (
        " ".join(full_tree.body.text(separator=" ", strip=True).split())
        if full_tree.body else ""
    )
    if not full:
        return ""

    tree = HTMLParser(html)
    for tag in CHROME_TAGS:
        for node in tree.css(tag):
            node.decompose()

    # Chrome is a minority of a page by definition. A wrapper whose class merely happens
    # to contain "header" can hold the entire article, so anything carrying a large share
    # of the text is left alone whatever it is called.
    budget = len(full) * 0.4
    for node in tree.css("[class]"):
        if node.tag in ("html", "body", "main", "article"):
            continue
        if not CHROME_CLASS_RE.search(node.attributes.get("class") or ""):
            continue
        if len(node.text(separator=" ", strip=True)) > budget:
            continue
        node.decompose()

    main = tree.css_first("main") or tree.css_first("article") or tree.body
    stripped = " ".join(main.text(separator=" ", strip=True).split()) if main else ""
    return full if len(stripped) < len(full) * min_retained else stripped


def detect_interfaces(html: str) -> list[str]:
    body = main_content_text(html)
    return sorted(
        name for name, pattern in INTERFACE_PATTERNS.items()
        if re.search(pattern, body, re.I)
    )


def heading_title(tree: HTMLParser) -> str:
    """The product name from ``<h1>``, without the marketing tagline.

    Vendors nest the tagline in a child element:

        <h1>PQ LC Series <span>Highly Sensitive LC-ICP-MS Solutions ...</span></h1>

    Taking the whole ``<h1>`` text produced names like "PQ LC Series Highly Sensitive
    LC-ICP-MS Solutions for the Determination of Elemental Species". Only the direct text
    nodes are the name; child elements are subtitles.
    """
    h1 = tree.css_first("h1")
    if h1 is None:
        return ""
    direct = " ".join(
        node.text(deep=False) for node in h1.iter(include_text=True) if node.tag == "-text"
    )
    direct = " ".join(direct.split())
    if len(direct) >= 3:
        return direct
    return " ".join(h1.text(separator=" ", strip=True).split())


def _meta(tree: HTMLParser, selector: str, attr: str = "content") -> str:
    node = tree.css_first(selector)
    return (node.attributes.get(attr) or "").strip() if node else ""


def category_from_url(url: str, recipe: Recipe | None) -> str:
    """Use the vendor's own taxonomy: the path segment above the product slug."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if len(segments) < 2:
        return ""
    # Keep the slug form ("icp-ms", not "icp ms"): it is a stable key for the controlled
    # vocabulary and matches the taxonomy the recipe already refers to.
    return segments[-2]


# A trailing vendor order code: at least two hyphen groups, so a model name like
# "BD056-230V" (one group) survives while "OL5004-26-027" and "818-08010-2" are cut.
_TRAILING_ORDER_RE = re.compile(r"\s*[A-Z]{0,4}\d[\dA-Za-z]*(?:-[\dA-Za-z]+){2,}\s*$")


_DECOR_RE = re.compile(r"\s*[▶►▸›»→➔]+\s*$")
_MARK_RE = re.compile(r"\s+([®™©])")


def clean_label(text: str) -> str:
    """Menu glyphs and detached trademark signs: "Formulator ®" -> "Formulator®"."""
    text = _DECOR_RE.sub("", text or "")
    text = _MARK_RE.sub(r"\1", text)
    text = re.sub(r"\s+TM\b", "™", text)
    return " ".join(text.split())


def clean_variant_name(raw: str) -> str:
    """Trim a spec-table row label down to a device name.

    Vendors put the whole marketing sentence in the row header:
    ``"PlasmaQuant MS - high sensitive, robust and reliable ICP-MS Instrument 818-08010-2"``
    becomes ``"PlasmaQuant MS"``.
    """
    name = clean_label(raw)
    name = _TRAILING_ORDER_RE.sub("", name)
    # Dashes only. Splitting on ";" / "," / ":" destroyed real distinctions:
    # "CyBio FeliX Basic Unit; Clean Bench" and "...; Clean Bench; with Light" both
    # collapsed to "CyBio FeliX Basic Unit", silently merging two different devices.
    for separator in (" | ", " - ", " – ", " — "):
        if separator in name:
            head = name.split(separator, 1)[0].strip()
            if len(head) >= 3:
                name = head
                break
    return name.strip(" -–—:,")


def device_name(raw: str, page_name: str, *, single: bool) -> str:
    """Pick the best available name for a device row."""
    cleaned = clean_variant_name(raw)
    if single:
        # One device on the page: the page title is the more complete, readable name,
        # but it still carries the vendor's title separator ("Model B 28 | Standard
        # Incubators with mechanical adjustment").
        return clean_variant_name(page_name) or cleaned
    if len(cleaned) >= 3:
        return cleaned
    return page_name


def product_id(manufacturer: str, url: str, name: str) -> str:
    vendor = re.sub(r"[^a-z0-9]+", "-", manufacturer.lower()).strip("-")
    slug = [s for s in urlparse(url).path.split("/") if s]
    tail = slug[-1] if slug else re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    suffix = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{vendor}__{tail}__{suffix}" if suffix and suffix != tail else f"{vendor}__{tail}"


def _specs_from(pairs: dict[str, str]) -> dict[str, SpecValue]:
    return {
        attribute_key(attr): parse_spec_value(value)
        for attr, value in pairs.items()
        if attr.strip() and value.strip() and attribute_key(attr)
    }


def extract_page(
    url: str,
    html: str,
    *,
    manufacturer: str,
    recipe: Recipe | None = None,
    category: str | None = None,
    name_hint: str = "",
) -> list[DeviceRecord]:
    """Extract every device described by one product page.

    ``category`` and ``name_hint`` come from how the page was reached — the vendor's
    menu label above it and the link text pointing to it. On flat sites the URL has no
    category segment at all, so the menu is the only place the vendor's taxonomy lives.
    """
    tree = HTMLParser(html)
    page_name = clean_label(heading_title(tree)) or clean_variant_name(name_hint)
    # Some h1s are taglines ("Step away from manual and repetitive work"). When the h1
    # shares no word with what the vendor's own menu calls the page, the menu wins.
    hint = clean_variant_name(name_hint)
    hint_ok = bool(hint) and not _CTA_NAME.fullmatch(hint)
    if _CTA_NAME.fullmatch(page_name or ""):
        title = tree.css_first("title")
        title_text = clean_variant_name(title.text(strip=True)) if title else ""
        page_name = hint if hint_ok else (title_text or page_name)
    glued = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    if hint_ok and page_name and not (_tokens(hint) & _tokens(page_name)) \
            and glued(hint) not in glued(page_name):
        page_name = hint
    # Still a tagline and no usable menu name ("Step away from manual and repetitive
    # work" on Tecan's Fluent Mix and Pierce page, reached via "Learn more"): the
    # <title> names the page. It wins when the h1 shares no word with it.
    title = tree.css_first("title")
    title_head = clean_variant_name(title.text(strip=True)) if title else ""
    content = lambda t: _tokens(t) - _STOPWORDS
    if title_head and page_name and len(page_name.split()) >= 4 \
            and not (content(title_head) & content(page_name)):
        page_name = title_head
    description = _meta(tree, 'meta[name="description"]') or _meta(
        tree, 'meta[property="og:description"]'
    )
    image = _meta(tree, 'meta[property="og:image"]')
    if image:
        image = urljoin(url, image)

    datasheets = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if _PDF_RE.search(href):
            absolute = urljoin(url, href)
            if absolute not in datasheets:
                datasheets.append(absolute)

    interfaces = detect_interfaces(html)
    category = clean_label(category or category_from_url(url, recipe))
    grid, provenance = find_spec_table(html, recipe)

    # Text specifications under a spec heading beat a table found without one, and
    # stand in when there is no table at all.
    section: dict[str, str] = {}
    if grid is None or provenance.startswith("validated-table"):
        section, section_heading = spec_section_pairs(html, recipe)
        table_attrs = len(to_spec_table(grid).attributes) if grid is not None else 0
        if len(section) >= 3 and len(section) >= table_attrs:
            grid, provenance = None, f"section:{section_heading}"
        else:
            section = {}

    common = dict(
        manufacturer=manufacturer,
        url=url,
        category=category,
        description=description,
        image_url=image,
        datasheet_urls=datasheets[:25],
        interfaces=interfaces,
        spec_source=provenance,
    )

    if section:
        name = clean_variant_name(page_name) or url.rstrip("/").rsplit("/", 1)[-1]
        return [
            DeviceRecord(
                product_id=product_id(manufacturer, url, name),
                name=name,
                specs=_specs_from(section),
                **common,
            )
        ]

    if grid is None:
        # Thin pages still produce a device; losing them silently is worse than a row
        # with no specs, and the warning makes the gap visible in review.
        name = page_name or url.rstrip("/").rsplit("/", 1)[-1]
        return [
            DeviceRecord(
                product_id=product_id(manufacturer, url, name),
                name=name,
                warnings=["no specification table found on page"],
                **common,
            )
        ]

    orientation = recipe.spec_table_orientation if recipe else None
    spec_table = to_spec_table(grid, orientation)

    # A multi-column table whose "devices" are not device names was read along the
    # wrong axis, or compares options of one device. Try the other axis; failing that,
    # keep every value as a spec of the single device the page is about.
    if len(spec_table.entities) > 1 and not plausible_device_names(
        spec_table.entities, page_name, name_hint
    ):
        flipped = to_spec_table(
            grid,
            "attribute_major" if spec_table.orientation == "variant_major" else "variant_major",
        )
        if plausible_device_names(flipped.entities, page_name, name_hint):
            spec_table = flipped
            common["spec_source"] = provenance + ":reoriented"
        else:
            flat = {
                f"{attr} ({entity})": value
                for entity, attrs in spec_table.records.items()
                for attr, value in attrs.items()
                if attr.strip() and value.strip()
            }
            common["spec_source"] = provenance + ":flattened"
            name = clean_variant_name(page_name) or url.rstrip("/").rsplit("/", 1)[-1]
            return [
                DeviceRecord(
                    product_id=product_id(manufacturer, url, name),
                    name=name,
                    specs=_specs_from(flat),
                    warnings=["table axis did not name devices; kept as one device"],
                    **common,
                )
            ]

    devices = group_devices(spec_table.records)

    # Name cleaning must never make two distinct devices indistinguishable. If it does,
    # fall back to the untrimmed labels for the colliding rows.
    proposed = [
        device_name(d.name, page_name, single=len(devices) == 1) for d in devices
    ]
    collisions = {n for n in proposed if proposed.count(n) > 1}

    records: list[DeviceRecord] = []
    for index, device in enumerate(devices):
        name = proposed[index]
        if name in collisions:
            # Fall back to the original table label, which is distinct by construction;
            # the canonical group name is not, since that is what collided.
            original = (device.source_variants or [device.name])[0]
            if original.strip():
                name = re.sub(r"\s+", " ", original.strip())
        if not name:
            name = f"{page_name} variant {index + 1}".strip()

        specs = {
            attribute_key(attr): parse_spec_value(value)
            for attr, value in device.specs.items()
            if attr.strip() and value.strip()
        }
        records.append(
            DeviceRecord(
                product_id=product_id(manufacturer, url, name),
                name=name,
                specs=specs,
                source_variants=device.source_variants,
                regional_variants=device.regional_variants,
                merge_reason=device.merge_reason,
                **common,
            )
        )
    return records
