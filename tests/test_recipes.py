"""Recipe loading and inference - the mechanism that keeps re-runs free of model calls."""

from __future__ import annotations

from prodscrape.recipes import infer_rules, load_recipe
from prodscrape.signals import page_signals


def test_saved_recipes_load():
    for domain, depth in (("analytik-jena.com", 5), ("binder-world.com", 6)):
        recipe = load_recipe(domain)
        assert recipe is not None, f"missing recipe for {domain}"
        assert recipe.family_depth == depth
        assert recipe.include
        assert recipe.inferred is False


def test_recipe_headings_reach_the_signal_extractor():
    """Regression: recipe spec/order headings were accepted but silently ignored, so a
    vendor-specific wording in a recipe changed nothing."""
    html = "<html><body><h2>Datos tecnicos</h2></body></html>"
    assert page_signals("https://x.test/a", html).has_spec_heading is False
    assert (
        page_signals(
            "https://x.test/a", html, spec_headings=["Datos tecnicos"]
        ).has_spec_heading
        is True
    )


def test_recipe_headings_extend_rather_than_replace_defaults():
    """A partial recipe must never make classification worse than the built-in defaults."""
    html = "<html><body><h2>Technical Data</h2></body></html>"
    sig = page_signals("https://x.test/a", html, spec_headings=["Datos tecnicos"])
    assert sig.has_spec_heading is True


def test_inference_finds_catalogue_root_and_depth():
    urls = [f"https://x.test/products/cat{i//10}/sub/model-{i}" for i in range(30)]
    urls += [f"https://x.test/products/cat{i}" for i in range(3)]
    urls += [f"https://x.test/news/post-{i}" for i in range(50)]
    guess = infer_rules(urls, "x.test")
    assert guess.inferred is True
    assert guess.include == ["/products/*"]
    assert guess.family_depth == 4


def test_inference_detects_locale_prefix():
    urls = [f"https://x.test/de-de/produkte/cat/sub/model-{i}" for i in range(20)]
    urls += [f"https://x.test/de-de/karriere/job-{i}" for i in range(5)]
    guess = infer_rules(urls, "x.test")
    assert guess.locale == "de-de"
    assert guess.include == ["/de-de/produkte/*"]


def test_inference_reports_uncertainty_when_no_product_token():
    urls = [f"https://x.test/sortiment/a/b/item-{i}" for i in range(20)]
    guess = infer_rules(urls, "x.test")
    assert any("verify this" in n for n in guess.notes)


def test_prodscrape_home_redirects_all_output(tmp_path, monkeypatch):
    """Installed via uvx there is no project directory; PRODSCRAPE_HOME decides where
    runs, caches and user recipes live."""
    from prodscrape import paths

    monkeypatch.setenv("PRODSCRAPE_HOME", str(tmp_path))
    assert paths.home() == tmp_path
    assert paths.runs_dir() == tmp_path / "runs"
    assert paths.cache_dir() == tmp_path / "cache"
    assert paths.user_recipe_dir() == tmp_path / "recipes"


def test_bundled_recipes_ship_with_the_package():
    """A uvx install has no checkout, so the vendor recipes must live in the package."""
    from prodscrape.paths import BUNDLED_RECIPE_DIR

    names = {p.name for p in BUNDLED_RECIPE_DIR.glob("*.yaml")}
    assert {"analytik-jena.com.yaml", "binder-world.com.yaml",
            "qinstruments.com.yaml"} <= names


def test_user_recipe_overrides_the_bundled_one(tmp_path, monkeypatch):
    from prodscrape.recipes import find_recipe, load_recipe
    from prodscrape.paths import BUNDLED_RECIPE_DIR

    monkeypatch.setenv("PRODSCRAPE_HOME", str(tmp_path))
    assert find_recipe("analytik-jena.com") == BUNDLED_RECIPE_DIR / "analytik-jena.com.yaml"

    user_dir = tmp_path / "recipes"
    user_dir.mkdir(parents=True)
    (user_dir / "analytik-jena.com.yaml").write_text(
        "domain: analytik-jena.com\nproduct_urls:\n  family_depth: 99\n  include: ['/x/*']\n",
        encoding="utf-8",
    )
    assert find_recipe("analytik-jena.com") == user_dir / "analytik-jena.com.yaml"
    assert load_recipe("analytik-jena.com").family_depth == 99


def test_checkout_keeps_artifacts_in_the_checkout(monkeypatch):
    """Working in the repo must behave exactly as before."""
    from prodscrape import paths

    monkeypatch.delenv("PRODSCRAPE_HOME", raising=False)
    assert (paths.home() / "pyproject.toml").exists()
