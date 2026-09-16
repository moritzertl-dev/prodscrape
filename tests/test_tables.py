"""Stage 3 regression tests - orientation detection is the load-bearing piece here.

Getting orientation wrong silently transposes every value in the table, producing a
plausible-looking but entirely wrong dataset. These tests cover both layouts.
"""

from __future__ import annotations

import pytest

from prodscrape.tables import (
    detect_orientation,
    parse_tables,
    to_spec_table,
    transpose,
)

# Variants down column 0, attributes across row 0 - the analytik-jena layout.
VARIANT_MAJOR = [
    ["", "Detector", "Oxide ratio", "Plasma Gas Flow", "Dimension (w x d x h)"],
    ["PlasmaQuant MS", "DDEM", "CeO+/Ce+ < 2 %", "7.5 - 10.5 L/min", "660 x 589 x 1131 mm"],
    ["PlasmaQuant MS Elite", "DDEM", "CeO+/Ce+ < 2 %", "7.5 - 10.5 L/min", "660 x 589 x 1131 mm"],
    ["PlasmaQuant MS Q", "DDEM", "CeO+/Ce+ < 2 %", "7.5 - 10.5 L/min", "660 x 589 x 1131 mm"],
]


def test_detects_variant_major():
    orientation, confidence, reasons = detect_orientation(VARIANT_MAJOR)
    assert orientation == "variant_major"
    assert confidence > 0.3
    assert any("common prefix" in r for r in reasons)


def test_detects_attribute_major_on_the_transpose():
    orientation, _, _ = detect_orientation(transpose(VARIANT_MAJOR))
    assert orientation == "attribute_major"


def test_both_layouts_normalise_to_the_same_records():
    """The whole point: layout must not change the extracted data."""
    a = to_spec_table(VARIANT_MAJOR)
    b = to_spec_table(transpose(VARIANT_MAJOR))
    assert a.records == b.records
    assert a.records["PlasmaQuant MS Elite"]["Plasma Gas Flow"] == "7.5 - 10.5 L/min"


def test_tiny_table_is_not_guessed_confidently():
    orientation, confidence, reasons = detect_orientation([["a"]])
    assert confidence == 0.0
    assert "too small" in reasons[0]


def test_recipe_supplied_orientation_overrides_detection():
    st = to_spec_table(VARIANT_MAJOR, orientation="attribute_major")
    assert st.orientation == "attribute_major"
    assert st.confidence == 1.0


def test_parses_real_page_tables(plasmaquant_html):
    grids = parse_tables(plasmaquant_html)
    assert len(grids) >= 10
    assert all(len({len(r) for r in g}) == 1 for g in grids), "grids must be rectangular"


def test_real_spec_table_orientation_and_values(plasmaquant_html):
    """The live Technical Data table: 4 instrument variants x 15 attributes."""
    grids = parse_tables(plasmaquant_html)
    spec = next(
        g for g in grids if any("Detector" in c for c in g[0])
    )
    st = to_spec_table(spec)

    assert st.orientation == "variant_major"
    assert len(st.entities) == 4
    assert all(e.startswith("PlasmaQuant MS") for e in st.entities)
    assert "Detector" in st.attributes

    elite = next(k for k in st.records if "Elite" in k and "Elite S" not in k)
    assert st.records[elite]["Cones/Interface"].startswith("Elite Cones Platinum")


def test_encoding_preserved_in_real_table(plasmaquant_html):
    """Spec sheets are full of µ, ±, ≤ - a blind decode corrupts values silently."""
    assert "µ" in plasmaquant_html


def test_order_information_table_yields_order_numbers(plasmaquant_html):
    grids = parse_tables(plasmaquant_html)
    order = next(g for g in grids if g[0][:2] == ["Order number", "Description"])
    numbers = [r[0] for r in order[1:]]
    assert len(numbers) == 4
    assert all(n.count("-") == 2 for n in numbers)


def test_row_header_cell_keeps_its_position():
    """Regression: selectolax's grouped `css("td, th")` returns all <td> then all <th>,
    which silently moves a leading <th scope="row"> to the end and shifts every value."""
    from prodscrape.tables import parse_tables

    html = "<table><tr><th>NAME</th><td>A</td><td>B</td></tr></table>"
    assert parse_tables(html) == [[["NAME", "A", "B"]]]
