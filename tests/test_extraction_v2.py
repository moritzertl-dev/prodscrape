"""Extraction beyond spec tables, and the guards that keep wrong tables out.

Each case is a page shape met on one of the six second-generation vendors.
"""

from __future__ import annotations

from prodscrape.extract import (
    _QUANTITY_RE, clean_label, extract_page, plausible_device_names, product_sections,
    spec_section_pairs,
)
from prodscrape.extract import DeviceRecord
from prodscrape.normalize import parse_spec_value
from prodscrape.pdfs import choose_datasheet
from prodscrape.pipeline import merge_cross_page_duplicates

ACCORDION = """
<html><body><h1>Mantis</h1>
<div class="pp-accordion-item"><div class="pp-accordion-button" role="button">
  <span class="pp-accordion-button-label">Specifications and Requirements</span></div>
  <div class="pp-accordion-content">
    <p><strong>Electrical Specifications</strong></p>
    <ul><li>110 V - 240 V, 50 Hz - 60 Hz, 50 W typical</li></ul>
    <p><strong>Physical Dimensions</strong></p>
    <ul><li>Height: 230 mm (Full Extension)</li><li>Height: 188 mm (No Extension)</li>
        <li>Weight: 5.2 kg</li></ul>
  </div></div>
<div class="pp-accordion-item"><div class="pp-accordion-button" role="button">
  <span class="pp-accordion-button-label">FAQs</span></div>
  <div><p>Question: is it good? Yes: 100 percent.</p></div></div>
</body></html>
"""


def test_specs_written_as_text_under_an_accordion_are_read():
    """Formulatrix: no <table>, no <h2> — an accordion label and bold sub-headings."""
    pairs, heading = spec_section_pairs(ACCORDION)
    assert heading == "Specifications and Requirements"
    assert pairs["Weight"] == "5.2 kg"
    # A repeated label is qualified by its sub-heading instead of overwriting.
    assert pairs["Height"].startswith("230 mm")
    assert pairs["Height (Physical Dimensions)"].startswith("188 mm")
    # Unlabelled lines are kept under their sub-heading, not dropped.
    assert "50 Hz" in pairs["Electrical Specifications"]
    # The next accordion section (FAQs) ends the spec section.
    assert not any("Question" in k for k in pairs)


def test_section_specs_become_a_device():
    [record] = extract_page("https://f.com/mantis", ACCORDION, manufacturer="F")
    assert record.name == "Mantis"
    assert record.spec_source.startswith("section:")
    assert "weight" in record.specs


RESOURCES_TABLE = """<html><body><h1>NT8</h1><h2>Resources</h2>
<table><tr><th></th><th>Wet Dispense</th><th>Dry Dispense</th></tr>
<tr><td>Guide</td><td>a</td><td>b</td></tr><tr><td>Video</td><td>c</td><td>d</td></tr>
<tr><td>Note</td><td>e</td><td>f</td></tr></table></body></html>"""


def test_tables_under_resource_headings_are_not_spec_tables():
    [record] = extract_page("https://f.com/nt8", RESOURCES_TABLE, manufacturer="F")
    assert record.name == "NT8" and not record.specs


def test_entity_names_must_name_devices():
    # A comparison table read along the wrong axis names attributes.
    assert not plausible_device_names(
        ["Lens/Objective Options", "Working Distance", "Effective N.A."], "Rock Imager")
    assert not plausible_device_names(["50", "15", "1.5"], "µPulse")
    assert not plausible_device_names(["10 µl", "50 µl", "200 µl"], "Air Displacement Pipettor")
    assert not plausible_device_names(["column_1", "column_2"], "Sunrise")
    # Real variants share the family name or carry a model designation.
    assert plausible_device_names(["ROCK IMAGER 1000", "ROCK IMAGER 2"], "Rock Imager")
    assert plausible_device_names(["qTOWER iris 384", "qTOWER iris 96"], "qTOWERiris Series")
    assert plausible_device_names(["AFU 3 Automatic Filtration Unit", "LS-T Sampler 2"], "AOX")


def test_quantities_are_not_model_names():
    for q in ("10 µl", "1.5 mL", "230 V", "96 wells"):
        assert _QUANTITY_RE.match(q), q
    for m in ("B 28", "PF400", "Echo 650"):
        assert not _QUANTITY_RE.match(m), m


PRECISEFLEX = """<html><body><h1>Recommended Products</h1>
<nav><a href="#pf400">PreciseFlex 400</a><a href="#pfc5">PreciseFlex c5</a>
<a href="#top">Back to top</a></nav>
<h2><a id="pf400"></a>PreciseFlex™400</h2><p>Four-axis robot.</p><ul><li>Payload: 1 kg</li></ul>
<h2><a id="pfc5"></a>PreciseFlex c5</h2><p>Collaborative.</p><ul><li>Vertical Reach: 400 mm</li>
<li>Discover the c5: Smarter automation</li></ul>
<footer>x</footer></body></html>"""

TABS = """<html><body><h1>BioArc Duo</h1>
<a href="#details">Details</a><a href="#parts">Part Numbers</a><a href="#versions">Versions</a>
<div id="details">...</div><div id="parts">...</div><div id="versions">...</div></body></html>"""


def test_multi_product_pages_are_split_into_devices():
    """PreciseFlex lists five robots as in-page sections with no page of their own."""
    sections = product_sections(PRECISEFLEX, "https://p.com/lab")
    assert [n for n, _ in sections] == ["PreciseFlex 400", "PreciseFlex c5"]


def test_in_page_tabs_are_not_products():
    """Azenta: Details / Part Numbers / Versions tabs use the same anchor mechanism."""
    assert product_sections(TABS, "https://a.com/p") == []


def test_trademark_and_menu_glyph_cleanup():
    assert clean_label("Formulator ®") == "Formulator®"
    assert clean_label("F.A.S.T. TM") == "F.A.S.T.™"
    assert clean_label("Protein Crystallization ▶") == "Protein Crystallization"


def _rec(name, url, **specs):
    return DeviceRecord(product_id=f"v__{name}__{url}", manufacturer="V", name=name,
                        url=url, specs={k: parse_spec_value(v) for k, v in specs.items()})


def test_one_device_on_two_pages_is_one_row():
    rows = merge_cross_page_duplicates([
        _rec("PreciseFlex c5", "https://p.com/lab", reach="400 mm"),
        _rec("PreciseFlex c5", "https://p.com/electronics", reach="400 mm", payload="5 kg"),
    ])
    assert len(rows) == 1 and set(rows[0].specs) == {"reach", "payload"}


def test_a_generic_shared_title_does_not_merge_different_machines():
    """Retsch titles every sieve shaker page "Vibrationssiebmaschine"."""
    rows = merge_cross_page_duplicates([
        _rec("Vibrationssiebmaschine", "https://r.com/as-200", amplitude="0-3 mm"),
        _rec("Vibrationssiebmaschine", "https://r.com/as-300", amplitude="0-2.2 mm"),
    ])
    assert len(rows) == 2


def test_datasheet_choice_prefers_the_devices_own_pdf_and_skips_chrome():
    urls = [
        "https://t.com/doc/tecan-training-catalog-brochure-pdf-395446",
        "https://t.com/doc/protein-quantification-application-note-pdf-1",
        "https://t.com/doc/infinite-200-pro-detection-brochure-pdf-396235",
    ]
    chosen = choose_datasheet(urls, "t.com", device_name="Infinite 200 PRO",
                              chrome={urls[0]})
    assert chosen == urls[2]
    assert choose_datasheet(urls[:2], "t.com", device_name="Spark", chrome={urls[0]}) is None
