"""Stage 3 — HTML table parsing with orientation detection.

Vendors lay spec tables out in two ways and both are common:

``attribute_major``  attributes run down column 0, variants across row 0 (the classic form)
``variant_major``    variants run down column 0, attributes across row 0

analytik-jena.com uses ``variant_major`` — the PlasmaQuant MS page puts four instrument
variants in rows and fifteen spec attributes in columns. Assuming either layout would
silently transpose every extracted value, so orientation is *detected*, not configured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from os.path import commonprefix

from selectolax.parser import HTMLParser

_WS_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d")


def _cell_text(node) -> str:
    return _WS_RE.sub(" ", node.text(separator=" ", strip=True)).strip()


def _row_cells(row) -> list[str]:
    """Cells of a ``<tr>`` in document order.

    Do *not* use ``row.css("td, th")``: selectolax returns grouped selector matches
    per-selector, not in document order, so a leading ``<th scope="row">`` product-name
    cell is silently moved to the end of the row and every value shifts one column left.
    That produces a plausible-looking, entirely wrong table.
    """
    return [_cell_text(c) for c in row.iter() if c.tag in ("td", "th")]


def parse_tables(html: str) -> list[list[list[str]]]:
    """Extract every ``<table>`` as a rectangular grid of cell strings."""
    tree = HTMLParser(html)
    grids = []
    for table in tree.css("table"):
        grid = []
        for row in table.css("tr"):
            cells = _row_cells(row)
            if cells:
                grid.append(cells)
        if grid:
            width = max(len(r) for r in grid)
            grid = [r + [""] * (width - len(r)) for r in grid]
            grids.append(grid)
    return grids


def _digit_fraction(cells: list[str]) -> float:
    cells = [c for c in cells if c]
    if not cells:
        return 0.0
    return sum(1 for c in cells if _DIGIT_RE.search(c)) / len(cells)


def _shared_prefix_ratio(cells: list[str]) -> float:
    """How much of each cell is a prefix shared with its siblings.

    Product variants almost always share a family name ("PlasmaQuant MS", "PlasmaQuant MS
    Elite", "PlasmaQuant MS Q"), whereas attribute labels are unrelated to one another.
    This is the single most reliable orientation signal.
    """
    cells = [c for c in cells if c]
    if len(cells) < 2:
        return 0.0
    prefix = commonprefix(cells).strip()
    if len(prefix) < 3:
        return 0.0
    return len(prefix) / (sum(len(c) for c in cells) / len(cells))


# Rows whose values name the variants rather than describing a specification. Ordered by
# preference: a human-readable designation beats an internal article number.
ENTITY_LABEL_ROWS = (
    "designation",
    "model",
    "type",
    "product",
    "name",
    "variant",
    "order number",
    "article number",
)


@dataclass
class SpecTable:
    """A parsed spec table normalised to entity -> {attribute: value}."""

    orientation: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    records: dict[str, dict[str, str]] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)


def detect_orientation(grid: list[list[str]]) -> tuple[str, float, list[str]]:
    """Decide which axis carries attribute names.

    Returns ``(orientation, confidence, reasons)``. Reasons are persisted so a wrong call
    is debuggable after the fact rather than mysterious.
    """
    if len(grid) < 2 or len(grid[0]) < 2:
        return "attribute_major", 0.0, ["table too small to judge"]

    row0 = grid[0][1:]
    col0 = [r[0] for r in grid[1:]]

    # Two structural signals that settle the question outright, checked before the
    # statistical heuristics because a blank header row defeats those.
    #
    # BINDER's spec tables open with a section row like ["Data", "", ""] and put the
    # variant names in a later "Designation" row. Judging by cell length then made the
    # empty header row look like a terse attribute axis and flipped the whole table.
    if all(not c.strip() for c in row0):
        return "attribute_major", 0.9, ["row 0 carries no labels beyond the corner cell"]

    section_rows = sum(
        1 for r in grid[1:] if r[0].strip() and not any(c.strip() for c in r[1:])
    )
    if section_rows >= 2:
        return (
            "attribute_major",
            0.85,
            [f"{section_rows} section header rows — attributes are grouped down column 0"],
        )

    reasons: list[str] = []
    score = 0.0  # positive favours variant_major

    # An empty top-left corner is the canonical matrix header; both axes are labelled.
    if not grid[0][0].strip():
        score += 0.15
        reasons.append("empty top-left corner cell")

    col0_prefix = _shared_prefix_ratio(col0)
    row0_prefix = _shared_prefix_ratio(row0)
    if col0_prefix > row0_prefix + 0.15:
        score += 0.45
        reasons.append(f"column 0 entries share a common prefix ({col0_prefix:.2f})")
    elif row0_prefix > col0_prefix + 0.15:
        score -= 0.45
        reasons.append(f"row 0 entries share a common prefix ({row0_prefix:.2f})")

    row0_digits = _digit_fraction(row0)
    col0_digits = _digit_fraction(col0)
    if row0_digits + 0.15 < col0_digits:
        score += 0.25
        reasons.append(
            f"row 0 is more label-like (digits {row0_digits:.2f} vs {col0_digits:.2f})"
        )
    elif col0_digits + 0.15 < row0_digits:
        score -= 0.25
        reasons.append(
            f"column 0 is more label-like (digits {col0_digits:.2f} vs {row0_digits:.2f})"
        )

    # Attribute labels are terse; spec values and product blurbs run long.
    row0_len = sum(len(c) for c in row0) / max(len(row0), 1)
    col0_len = sum(len(c) for c in col0) / max(len(col0), 1)
    if row0_len + 10 < col0_len:
        score += 0.2
        reasons.append(f"row 0 cells are shorter ({row0_len:.0f} vs {col0_len:.0f} chars)")
    elif col0_len + 10 < row0_len:
        score -= 0.2
        reasons.append(f"column 0 cells are shorter ({col0_len:.0f} vs {row0_len:.0f} chars)")

    orientation = "variant_major" if score > 0 else "attribute_major"
    return orientation, min(abs(score), 1.0), reasons


def to_spec_table(grid: list[list[str]], orientation: str | None = None) -> SpecTable:
    """Normalise a grid into entity -> {attribute: value} regardless of layout."""
    if orientation is None:
        orientation, confidence, reasons = detect_orientation(grid)
    else:
        confidence, reasons = 1.0, ["orientation supplied by recipe"]

    sections: dict[str, str] = {}

    if orientation == "variant_major":
        attributes = grid[0][1:]
        entities = [r[0] for r in grid[1:]]
        records = {
            row[0]: {attributes[i]: row[i + 1] for i in range(len(attributes))}
            for row in grid[1:]
        }
    else:
        entities = grid[0][1:]
        attributes = []
        current_section = ""
        data_rows = []
        for row in grid[1:]:
            label = row[0].strip()
            if label and not any(c.strip() for c in row[1:]):
                # A section header ("Electrical data"), not a specification.
                current_section = label
                continue
            if not label:
                continue
            attributes.append(label)
            sections[label] = current_section
            data_rows.append(row)

        # The variant names may not be in the header at all; recover them from a
        # designation-style row so downstream device identity has something to key on.
        if all(not e.strip() for e in entities):
            for key in ENTITY_LABEL_ROWS:
                match = next(
                    (r for r in data_rows if r[0].strip().lower() == key), None
                )
                if match and any(c.strip() for c in match[1:]):
                    entities = list(match[1:])
                    reasons.append(f"variant names recovered from the {match[0]!r} row")
                    break

        if all(not e.strip() for e in entities):
            entities = [f"column_{i + 1}" for i in range(len(grid[0]) - 1)]
            reasons.append("variant names unavailable; columns numbered")

        records = {
            ent: {row[0].strip(): row[i + 1] for row in data_rows}
            for i, ent in enumerate(entities)
        }

    return SpecTable(
        orientation=orientation,
        confidence=confidence,
        reasons=reasons,
        attributes=attributes,
        entities=entities,
        records=records,
        sections=sections,
    )


def transpose(grid: list[list[str]]) -> list[list[str]]:
    return [list(col) for col in zip(*grid)]
