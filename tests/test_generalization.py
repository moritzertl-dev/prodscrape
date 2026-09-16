"""Guards against overfitting to the vendors the tool was developed on.

Each test here encodes a failure seen on a real site that the first three vendors never
exhibited.
"""

from __future__ import annotations

from prodscrape.discover import CATALOGUE_HINTS, crawl_priority
from prodscrape.extract import _looks_like_non_spec
from prodscrape.recipes import infer_rules
from prodscrape.signals import ORDER_NUMBER_RE, find_order_numbers, looks_like_date


# --- order codes -------------------------------------------------------------------

def test_order_codes_are_vendor_neutral():
    """Was `\d{3}-\d{4,6}-\d` — Analytik Jena's format and nobody else's."""
    for code in ("818-08010-2", "OL5004-26-027", "20.745.0001", "AB1/234/X9"):
        assert ORDER_NUMBER_RE.search(code), code


def test_dates_are_not_order_codes():
    """Dates share the shape exactly; unfiltered they gave a press release the same
    'has order numbers' signal as a product page."""
    for date in ("2018-01-26", "2021-10-28T10", "31.03.2024"):
        assert looks_like_date(date), date
    found = find_order_numbers("published 2024-03-31, order 818-08010-2 today")
    assert found == ["818-08010-2"]


# --- non-spec tables ---------------------------------------------------------------

def test_downloads_table_rejected_by_language_column():
    """Structural, so it works on any vendor in any language."""
    grid = [["Titel", "Sprache", "Info"],
            ["Broschüre A", "de", "PDF, 1 MB"],
            ["Brochure A", "en", "PDF, 1 MB"]]
    assert _looks_like_non_spec(grid) is True


def test_spec_table_with_an_innocent_word_is_kept():
    """Regression: a single weak word in row 0 rejected 39 real Analytik Jena spec
    tables, because in a variant-major table row 0 holds attribute names."""
    grid = [["", "Sample size", "Detector", "Format", "Throughput", "Weight"],
            ["Model A", "10 mL", "DDEM", "SBS", "96/h", "12 kg"],
            ["Model B", "20 mL", "DDEM", "SBS", "192/h", "14 kg"]]
    assert _looks_like_non_spec(grid) is False


def test_narrow_table_with_two_weak_words_is_rejected():
    grid = [["Product", "Required", "Included"],
            ["Thing", "yes", "no"],
            ["Other", "no", "yes"]]
    assert _looks_like_non_spec(grid) is True


# --- crawl ordering ----------------------------------------------------------------

def test_catalogue_paths_are_crawled_before_editorial_ones():
    """A bounded crawl spends its budget on whatever it visits first."""
    assert crawl_priority("https://x.test/products/mill") < crawl_priority("https://x.test/about")
    assert crawl_priority("https://x.test/produkte/x") < crawl_priority("https://x.test/news/y")
    assert crawl_priority("https://x.test/misc") < crawl_priority("https://x.test/press/z")


def test_catalogue_hints_cover_more_than_english():
    assert {"produkt", "produit", "producto", "katalog"} <= set(CATALOGUE_HINTS)


# --- inference ---------------------------------------------------------------------

def test_locale_and_root_are_chosen_jointly():
    """Retsch translates path segments per locale: /bg/products/ but /de/produkte/.
    Picking the commonest locale and the commonest root separately produced
    /de/products/ — a path that exists nowhere on the site."""
    urls = (
        [f"https://x.test/bg/products/cat/sub/model-{i}" for i in range(30)]
        + [f"https://x.test/de/produkte/cat/sub/modell-{i}" for i in range(40)]
    )
    guess = infer_rules(urls, "x.test")
    assert guess.include == ["/de/produkte/*"]
    assert guess.family_depth == 5


def test_inference_handles_sites_with_no_locale_prefix():
    urls = [f"https://x.test/products/cat/model-{i}" for i in range(20)]
    guess = infer_rules(urls, "x.test")
    assert guess.include == ["/products/*"]
    assert guess.locale is None


def test_uncommon_locales_are_recognised():
    from prodscrape.recipes import LOCALE_SEGMENTS

    assert {"bg", "cz", "hu", "tr", "int-en"} <= LOCALE_SEGMENTS
