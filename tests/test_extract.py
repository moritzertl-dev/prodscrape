"""Stage 3-4 tests: value parsing, spec-table selection, naming, export shapes."""

from __future__ import annotations

import json

import pytest

from prodscrape.export import to_eav, to_wide
from prodscrape.extract import (
    clean_variant_name,
    detect_interfaces,
    extract_page,
    find_spec_table,
    tables_with_headings,
)
from prodscrape.fetch import Cache
from prodscrape.normalize import attribute_key, parse_number, parse_spec_value

PLASMAQUANT = (
    "https://www.analytik-jena.com/products/chemical-analysis/"
    "elemental-analysis/icp-ms/plasmaquant-ms-series/"
)


# --- normalisation -----------------------------------------------------------------

def test_parse_german_decimal_comma():
    assert parse_number("0,25") == 0.25
    assert parse_number("1.234,5") == 1234.5
    assert parse_number("1,234") == 1234      # three trailing digits = thousands
    assert parse_number("12.5") == 12.5


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("30...70 °C", "range"),
        ("7.5 - 10.5 L/min", "range"),
        ("660 mm x 589 mm x 1131 mm", "dimensions"),
        ("0,25 kW", "number"),
        ("95 min", "number"),
        ("yes", "boolean"),
        ("-", "boolean"),
        ("Discrete dynode electron multiplier", "text"),
    ],
)
def test_spec_value_kinds(raw, kind):
    assert parse_spec_value(raw).kind == kind


def test_range_keeps_bounds_and_unit():
    v = parse_spec_value("30...70 °C")
    assert (v.value_min, v.value_max) == (30.0, 70.0)
    assert v.unit == "°C"


def test_dimensions_capture_all_three():
    v = parse_spec_value("660 mm x 589 mm x 1131 mm")
    assert v.values == [660.0, 589.0, 1131.0]


def test_raw_text_is_always_retained():
    """Every spec keeps its source text so a bad parse stays recoverable."""
    for raw in ("30...70 °C", "CeO+/Ce+ < 2 %", "whatever"):
        assert parse_spec_value(raw).raw == raw


def test_attribute_key_is_stable_snake_case():
    assert attribute_key("Dimension (width x depth x height)") == "dimension_width_x_depth_x_height"
    assert attribute_key("Temperature range") == "temperature_range"


# --- naming ------------------------------------------------------------------------

def test_clean_variant_name_strips_order_codes_but_keeps_models():
    assert clean_variant_name("CyBio QuadPrint HQ-M OL5004-26-027") == "CyBio QuadPrint HQ-M"
    assert clean_variant_name("PlasmaQuant MS - high sensitive ICP-MS 818-08010-2") == "PlasmaQuant MS"
    # A model name with a single hyphen group must survive untouched.
    assert clean_variant_name("BD056-230V") == "BD056-230V"
    assert clean_variant_name("BioShake 3000 elm") == "BioShake 3000 elm"


# --- spec table selection ----------------------------------------------------------

def test_tables_are_paired_with_the_heading_above_them():
    html = ("<h2>Technical Data</h2><table><tr><td>a</td><td>b</td></tr></table>"
            "<h2>Downloads</h2><table><tr><td>c</td><td>d</td></tr></table>")
    pairs = tables_with_headings(html)
    assert [h for h, _ in pairs] == ["Technical Data", "Downloads"]


def test_order_number_catalogue_is_rejected():
    """Regression: falling back to the largest table pulled in analytik-jena's 111-row
    accessory catalogues and produced 425 phantom devices."""
    rows = "".join(
        f"<tr><td>418-880{i:02d}-0</td><td>Accessory {i}</td><td>x</td></tr>"
        for i in range(12)
    )
    html = f"<h2>Overview</h2><table><tr><td>Order</td><td>Desc</td><td>x</td></tr>{rows}</table>"
    grid, provenance = find_spec_table(html)
    assert grid is None
    assert provenance == "none"


def test_no_spec_table_is_preferred_over_the_wrong_one(plasmaquant_html):
    """Only heading-matched or structurally validated tables are accepted."""
    _, provenance = find_spec_table(plasmaquant_html)
    assert provenance.startswith("heading:")
    assert "Technical Data" in provenance


# --- end to end on a real page -----------------------------------------------------

def test_extract_plasmaquant_yields_four_devices(plasmaquant_html):
    records = extract_page(PLASMAQUANT, plasmaquant_html, manufacturer="Analytik Jena")
    assert len(records) == 4
    names = [r.name for r in records]
    assert all(n.startswith("PlasmaQuant MS") for n in names), names
    assert all(r.specs for r in records)

    elite = next(r for r in records if r.name.endswith("Elite"))
    assert elite.specs["cones_interface"].raw.startswith("Elite Cones Platinum")
    assert elite.category == "icp-ms"
    assert elite.url == PLASMAQUANT
    assert elite.datasheet_urls


def test_extracted_specs_keep_units(plasmaquant_html):
    records = extract_page(PLASMAQUANT, plasmaquant_html, manufacturer="Analytik Jena")
    dims = records[0].specs["dimension_width_x_depth_x_height"]
    assert dims.kind == "dimensions"
    assert dims.values == [660.0, 589.0, 1131.0]


def test_interface_detection():
    assert "Ethernet" in detect_interfaces("<body>Connection via RJ-45 Ethernet port</body>")
    assert "RS-232" in detect_interfaces("<body>RS 232 serial interface</body>")
    assert detect_interfaces("<body>nothing relevant here</body>") == []


# --- export ------------------------------------------------------------------------

def test_export_shapes(plasmaquant_html):
    records = extract_page(PLASMAQUANT, plasmaquant_html, manufacturer="Analytik Jena")

    wide = to_wide(records)
    assert len(wide) == 4
    assert set(wide[0]) >= {"product_id", "name", "category", "url", "interfaces", "specs"}
    # The specs bag must be valid JSON a downstream agent or RAG can read.
    assert json.loads(wide[0]["specs"])

    eav = to_eav(records)
    assert len(eav) == sum(len(r.specs) for r in records)
    assert {"product_id", "attribute", "value", "unit", "raw", "source_url"} <= set(eav[0])
    # Losslessness: every EAV row carries the original text.
    assert all(row["raw"] for row in eav)


def test_large_numbers_are_not_truncated():
    """Regression: a three-digit cap parsed "1131 mm" as 113 — plausible and invisible."""
    assert parse_spec_value("1131 mm").value == 1131.0
    assert parse_spec_value("5115 amu/s").value == 5115.0
    assert parse_spec_value("660 mm x 589 mm x 1131 mm").values == [660.0, 589.0, 1131.0]
    assert parse_spec_value("12500 rpm").value == 12500.0


def test_range_with_trailing_parenthetical_still_parses():
    v = parse_spec_value("200 to 3,000 rpm ( Maximum allowed mass 300 g)")
    assert v.kind == "range"
    assert (v.value_min, v.value_max) == (200.0, 3000.0)
    assert v.unit == "rpm"


def test_value_with_trailing_prose_stays_text():
    """Half-reading a value is worse than not reading it; raw is always kept."""
    v = parse_spec_value("2.5 mm, 4.0 mm and 6.1 mm")
    assert v.kind == "text"
    assert v.raw == "2.5 mm, 4.0 mm and 6.1 mm"


def test_heading_title_drops_the_marketing_tagline():
    """Vendors nest the tagline in a child element of <h1>."""
    from selectolax.parser import HTMLParser
    from prodscrape.extract import heading_title

    html = ('<h1>PQ LC Series <span class="h5">Highly Sensitive LC-ICP-MS Solutions '
            'for the Determination of Elemental Species</span></h1>')
    assert heading_title(HTMLParser(html)) == "PQ LC Series"


def test_heading_title_falls_back_to_full_text():
    from selectolax.parser import HTMLParser
    from prodscrape.extract import heading_title

    assert heading_title(HTMLParser("<h1><span>BioShake Q1</span></h1>")) == "BioShake Q1"


def test_records_without_specs_are_not_device_rows(plasmaquant_html):
    """Series/overview pages must not reach devices.csv - but must not vanish either."""
    from prodscrape.export import is_device_row, to_review_queue
    from prodscrape.extract import DeviceRecord

    series = DeviceRecord(
        product_id="x", manufacturer="m", name="AOX Autosampler Series", url="u",
        warnings=["no specification table found on page"],
    )
    real = extract_page(PLASMAQUANT, plasmaquant_html, manufacturer="Analytik Jena")[0]

    assert is_device_row(series) is False
    assert is_device_row(real) is True

    queued = to_review_queue([series])
    assert queued[0]["name"] == "AOX Autosampler Series"
    assert queued[0]["verdict"] == ""          # awaiting a judgment call
    assert "no specification table" in queued[0]["reason"]


def test_device_table_carries_no_scrape_metadata(plasmaquant_html):
    """The deliverable table describes devices, not how the scrape went."""
    from prodscrape.export import CORE_COLUMNS

    records = extract_page(PLASMAQUANT, plasmaquant_html, manufacturer="Analytik Jena")
    row = to_wide(records)[0]
    for leaked in ("spec_count", "variants_merged", "datasheet_count", "spec_source",
                   "warnings", "merge_reason", "source_variants"):
        assert leaked not in row, leaked
    assert set(row) == set(CORE_COLUMNS)


def test_semicolon_configurations_are_not_collapsed():
    """Regression: splitting on ';' merged 'Basic Unit; Clean Bench' and
    'Basic Unit; Clean Bench; with Light' into one indistinguishable name."""
    a = clean_variant_name("CyBio FeliX Basic Unit; Clean Bench OL5015-25-501")
    b = clean_variant_name("CyBio FeliX Basic Unit; Clean Bench; with Light OL5015-25-502")
    assert a != b
    assert a == "CyBio FeliX Basic Unit; Clean Bench"


def test_name_collisions_fall_back_to_full_labels():
    """Cleaning must never make two distinct devices look identical."""
    from prodscrape.extract import extract_page

    html = """<h1>Widget</h1><h2>Technical Data</h2><table>
      <tr><th></th><th>Power</th><th>Speed</th><th>Mass</th></tr>
      <tr><th>Unit - alpha</th><td>1</td><td>2</td><td>3</td></tr>
      <tr><th>Unit - beta</th><td>9</td><td>8</td><td>7</td></tr>
    </table>"""
    records = extract_page("https://x.test/a/b", html, manufacturer="M")
    names = [r.name for r in records]
    assert len(names) == len(set(names)), names


def test_title_separator_is_stripped_from_page_names():
    """BINDER h1: "Model B 28 | Standard-Incubators with mechanical adjustment"."""
    assert clean_variant_name(
        "Model B 28 | Standard-Incubators with mechanical adjustment"
    ) == "Model B 28"
    assert clean_variant_name("PQ LC Series") == "PQ LC Series"


def test_single_device_spec_table_is_accepted_under_a_heading():
    """Regression: a page describing one device has a two-row spec table (header + one
    row). A flat "at least 3 rows" rule rejected real instruments — multi X 2500's
    Technical Data table is 2x13."""
    html = ("<h1>multi X 2500</h1><h2>Technical Data</h2><table>"
            "<tr><th></th><th>Detection</th><th>Range</th><th>Furnace</th></tr>"
            "<tr><th>multi X 2500</th><td>coulometry</td><td>0.5-500 ug</td><td>950 C</td></tr>"
            "</table>")
    grid, provenance = find_spec_table(html)
    assert grid is not None
    assert provenance.startswith("heading:")


def test_tiny_table_without_a_spec_heading_is_still_rejected():
    """The relaxed size bar applies only under a spec heading."""
    html = ("<h2>Contact us</h2><table>"
            "<tr><th></th><th>a</th></tr><tr><th>row</th><td>b</td></tr></table>")
    grid, provenance = find_spec_table(html)
    assert grid is None
    assert provenance == "none"
