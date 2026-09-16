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
from .signals import SPEC_HEADINGS
from .tables import _row_cells, detect_orientation, to_spec_table

# Tables that look structurally like spec tables but are not.
NON_SPEC_HEADERS = (
    ("title", "language", "info"),
    ("order number", "description"),
    ("category", "description", "status"),
    ("product", "required", "included"),
    ("designation", "order number"),
)

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
    "Serial": r"\bserial\s+(?:port|interface)\b",
    "Analog I/O": r"\banalog\s+(?:in|out|i/o)\b",
    "Digital I/O": r"\bdigital\s+(?:in|out|i/o)\b",
}

_PDF_RE = re.compile(r"\.pdf(\?|$)", re.I)


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
    header = tuple(c.strip().lower() for c in grid[0][:3])
    return any(all(h in header for h in bad[: len(header)]) for bad in NON_SPEC_HEADERS)


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
    return out


ORDER_NUMBER_CELL_RE = re.compile(r"^\s*\d{3}-\d{4,6}-\d\s*$")

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
        hits = sum(1 for c in cells if ORDER_NUMBER_CELL_RE.match(c))
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


def detect_interfaces(html: str) -> list[str]:
    text = HTMLParser(html).body
    body = text.text(separator=" ", strip=True) if text else ""
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


def clean_variant_name(raw: str) -> str:
    """Trim a spec-table row label down to a device name.

    Vendors put the whole marketing sentence in the row header:
    ``"PlasmaQuant MS - high sensitive, robust and reliable ICP-MS Instrument 818-08010-2"``
    becomes ``"PlasmaQuant MS"``.
    """
    name = re.sub(r"\s+", " ", (raw or "").strip())
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


def extract_page(
    url: str,
    html: str,
    *,
    manufacturer: str,
    recipe: Recipe | None = None,
) -> list[DeviceRecord]:
    """Extract every device described by one product page."""
    tree = HTMLParser(html)
    page_name = heading_title(tree)
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
    category = category_from_url(url, recipe)
    grid, provenance = find_spec_table(html, recipe)

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
