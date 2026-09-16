"""Device identity rules, tested against real cached vendor tables.

The rule: one row per device; collapse only when every differing attribute is
non-functional (mains voltage, frequency, article number, designation).
"""

from __future__ import annotations

import json

import pytest

from prodscrape.devices import (
    canonical_name,
    compare_variants,
    group_devices,
    is_cosmetic_attribute,
)
from prodscrape.fetch import Cache
from prodscrape.tables import parse_tables, to_spec_table


def _cached_page(domain: str, slug: str) -> str:
    cache = Cache(f"cache/{domain}")
    for line in open(f"cache/{domain}/index.jsonl", encoding="utf-8"):
        rec = json.loads(line)
        if rec["url"].rstrip("/").endswith(slug):
            return open(rec["body_path"], encoding="utf-8").read()
    pytest.skip(f"{domain}{slug} not in cache")


def test_cosmetic_attribute_classification():
    for name in ("Rated Voltage", "Power frequency", "Article Number", "Designation",
                 "Phase (Nominal voltage)", "Order number", "Colour"):
        assert is_cosmetic_attribute(name), name
    for name in ("Temperature range", "Interior volume", "Net weight of the unit (empty)",
                 "Number of shelves (std./max.)", "Detector", "Operating voltage range"):
        assert not is_cosmetic_attribute(name), name


def test_voltage_variants_merge_into_one_device():
    """The exact case from the brief: a US 120 V unit is not a separate device."""
    records = {
        "B028-230V": {"Rated Voltage": "230 V", "Power frequency": "50/60 Hz",
                      "Article Number": "9010-0002", "Temperature range": "30...70 C"},
        "B028-120V": {"Rated Voltage": "120 V", "Power frequency": "60 Hz",
                      "Article Number": "9010-0067", "Temperature range": "30...70 C"},
    }
    devices = group_devices(records)
    assert len(devices) == 1
    assert devices[0].name == "B028"
    assert devices[0].is_merged
    assert "Rated Voltage" in devices[0].merge_reason
    # The functional spec stays shared; the differing values are kept per variant.
    assert devices[0].specs["Temperature range"] == "30...70 C"
    assert {v["_variant"] for v in devices[0].regional_variants} == {"B028-230V", "B028-120V"}


def test_functional_difference_keeps_devices_apart():
    records = {
        "BioShake 3000": {"Mixing lock": "fixed", "Rated Voltage": "230 V"},
        "BioShake 3000 elm": {"Mixing lock": "exchangeable magnetic", "Rated Voltage": "230 V"},
    }
    devices = group_devices(records)
    assert len(devices) == 2
    assert all(not d.is_merged for d in devices)


def test_real_binder_table_collapses_to_one_device():
    """B028-230V / B028-120V from the live page: 26 attributes, 4 differ, all cosmetic."""
    html = _cached_page("binder-world.com", "/b-28")
    grid = next(g for g in parse_tables(html) if any("230V" in v for r in g for v in r))
    st = to_spec_table(grid)

    assert st.orientation == "attribute_major"
    assert st.entities == ["B028-230V", "B028-120V"]

    decision = compare_variants(st.records["B028-230V"], st.records["B028-120V"])
    assert decision.merged is True
    assert set(decision.functional) == set()
    assert "Rated Voltage" in decision.cosmetic
    assert "Power frequency" in decision.cosmetic

    devices = group_devices(st.records)
    assert len(devices) == 1
    assert devices[0].name == "B028"


def test_real_plasmaquant_table_stays_four_devices():
    """PlasmaQuant MS / Elite / Elite S / Q differ in detector, cones and pump."""
    cache = Cache("tests/golden/cache")
    url = ("https://www.analytik-jena.com/products/chemical-analysis/"
           "elemental-analysis/icp-ms/plasmaquant-ms-series/")
    html = cache.get(url)[1]
    grid = next(g for g in parse_tables(html) if any("Detector" in c for c in g[0]))
    st = to_spec_table(grid)

    devices = group_devices(st.records)
    assert len(devices) == 4, [d.name for d in devices]
    assert all(not d.is_merged for d in devices)


def test_canonical_name_strips_regional_suffix():
    assert canonical_name(["B028-230V", "B028-120V"]) == "B028"
    assert canonical_name(["BD056-230V", "BD056UL-120V"]) == "BD056"
    assert canonical_name(["BioShake 3000"]) == "BioShake 3000"


def test_identical_variants_merge():
    records = {"A-230V": {"x": "1"}, "A-120V": {"x": "1"}}
    devices = group_devices(records)
    assert len(devices) == 1
    assert devices[0].merge_reason == "identical specifications"


def test_identical_specs_but_unrelated_names_do_not_merge():
    """Two differently named products that happen to list the same specs stay apart."""
    records = {"Alpha": {"x": "1"}, "Beta": {"x": "1"}}
    assert len(group_devices(records)) == 2


def test_software_only_difference_is_not_a_new_device():
    """Only functional *hardware* differences create a separate device.

    Names are held equivalent here so the attribute rule is what is under test; a name
    that differs by more than a regional token blocks the merge on its own.
    """
    records = {
        "Unit X-230V": {"Bundled software": "Basic", "Warranty": "12 months",
                        "Temperature range": "5...70 C"},
        "Unit X-120V": {"Bundled software": "Pro", "Warranty": "24 months",
                        "Temperature range": "5...70 C"},
    }
    devices = group_devices(records)
    assert len(devices) == 1


def test_added_hardware_module_is_a_new_device():
    """BINDER 'KB PRO 260 with ICH light module' vs plain 'KB PRO 260'."""
    records = {
        "KB PRO 260": {"Light module": "", "Interior volume": "247 l"},
        "KB PRO 260 with ICH light module": {"Light module": "ICH compliant",
                                             "Interior volume": "247 l"},
    }
    assert len(group_devices(records)) == 2


def test_orbit_diameter_is_a_new_device():
    records = {
        "BioShake Q1": {"Shaking orbit": "2 mm", "Rated Voltage": "230 V"},
        "BioShake Q1 3.0 mm": {"Shaking orbit": "3 mm", "Rated Voltage": "230 V"},
    }
    assert len(group_devices(records)) == 2


def test_name_difference_blocks_a_merge_when_specs_are_identical():
    """Regression: 'qTOWER iris touch' and 'qTOWER iris' list identical specs because the
    touchscreen is never a table row. The name is the only evidence of a real hardware
    difference, so it must block the merge."""
    from prodscrape.devices import names_equivalent

    specs = {"Sample block": "Silver", "Block capacity": "96 well"}
    records = {
        "qTOWER iris touch, 230V, incl. color module 1 844-00855-2": dict(specs),
        "qTOWER iris, 230V, incl. color module 1 844-00853-2": dict(specs),
    }
    assert not names_equivalent(*records)
    assert len(group_devices(records)) == 2


def test_regional_tokens_are_stripped_before_comparing_names():
    from prodscrape.devices import names_equivalent, normalise_variant_name

    assert names_equivalent("B028-230V", "B028-120V")
    assert names_equivalent("BD056-230V", "BD056UL-120V")
    assert normalise_variant_name("B028-230V") == "b028"
    assert not names_equivalent("BioShake 3000", "BioShake 3000 elm")


def test_real_qtower_page_keeps_touch_variants_separate():
    """Live analytik-jena page: four listed variants, none of them duplicates."""
    html = _cached_page("analytik-jena.com", "/qtoweriris-series")
    grid = next(
        g for g in parse_tables(html)
        if any("qTOWER iris" in v for row in g for v in row) and len(g) > 3
    )
    devices = group_devices(to_spec_table(grid).records)
    names = [d.name for d in devices]
    assert len(names) == len(set(names)), names


def test_fuse_rating_is_a_regional_consequence_not_a_capability():
    """A 120 V build draws double the current, so its fuse rating differs while the
    machine is identical. BINDER's BD056-230V / BD056UL-120V differ only here."""
    records = {
        "BD056-230V": {"Unit fuse": "6,3 A", "Rated Voltage": "230 V",
                       "Interior volume": "62 l"},
        "BD056UL-120V": {"Unit fuse": "12,5 A", "Rated Voltage": "120 V",
                         "Interior volume": "62 l"},
    }
    devices = group_devices(records)
    assert len(devices) == 1
    assert devices[0].name == "BD056"
    # The differing values are preserved per variant, not discarded.
    assert any("12,5 A" in v.get("Unit fuse", "") for v in devices[0].regional_variants)


def test_capability_differences_still_separate_despite_equivalent_names():
    """Interior volume is a capability: same name pattern must not merge them."""
    records = {
        "BD056-230V": {"Interior volume": "62 l", "Rated Voltage": "230 V"},
        "BD056UL-120V": {"Interior volume": "115 l", "Rated Voltage": "120 V"},
    }
    assert len(group_devices(records)) == 2
