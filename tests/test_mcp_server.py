"""MCP surface: the contract an agent depends on.

The load-bearing property is that no tool returns bulk content — the agent must never be
handed a page.
"""

from __future__ import annotations

import asyncio
import json

from prodscrape import mcp_server
from prodscrape.digest import digest_size_estimate, page_digest
from prodscrape.verdicts import VerdictStore


def test_all_tools_register():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {
        "site_overview", "get_recipe", "put_recipe", "scan_site",
        "pending_classifications", "record_classifications", "extract_devices",
        "pending_reviews", "record_reviews", "device_table", "device_specs",
        "export_table", "run_status",
    } <= names


def test_every_tool_documents_itself():
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        assert tool.description and len(tool.description) > 40, tool.name


def test_page_digest_is_small(plasmaquant_html):
    """A 500 KB page must reach the agent as a few hundred tokens."""
    url = "https://www.analytik-jena.com/products/x/plasmaquant-ms-series/"
    digest = page_digest(url, plasmaquant_html)
    assert len(plasmaquant_html) > 400_000
    assert digest_size_estimate(digest) < 500
    # It still carries the evidence a judgment needs.
    assert digest["h1"].startswith("PlasmaQuant MS")
    assert digest["has_spec_heading"] is True
    assert digest["spec_table"]["variants"]


def test_digest_never_contains_raw_html(plasmaquant_html):
    digest = page_digest("https://x.test/a", plasmaquant_html)
    blob = json.dumps(digest)
    assert "<div" not in blob and "<script" not in blob and "</" not in blob


def test_verdict_store_round_trips(tmp_path):
    store = VerdictStore.load(tmp_path)
    store.set_classification("https://x.test/a", "consumable", "disposable tips")
    store.set_review("vendor__thing", "not-a-device", "series overview page")
    store.save()

    reloaded = VerdictStore.load(tmp_path)
    assert reloaded.classification_for("https://x.test/a")["label"] == "consumable"
    assert reloaded.classification_for("https://x.test/a")["decided_by"] == "model"
    assert reloaded.review_for("vendor__thing")["verdict"] == "not-a-device"
    assert reloaded.counts == {"classifications": 1, "reviews": 1}


def test_verdict_store_rejects_unknown_labels(tmp_path):
    store = VerdictStore.load(tmp_path)
    for bad in ("device", "gadget", ""):
        try:
            store.set_classification("u", bad, "r")
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid label {bad!r}")


def test_record_tools_report_errors_without_failing(tmp_path, monkeypatch):
    """A malformed verdict must not discard the good ones."""
    monkeypatch.setenv("PRODSCRAPE_HOME", str(tmp_path))
    result = mcp_server.record_classifications(
        "x.test",
        [
            {"url": "https://x.test/a", "label": "instrument", "reason": "ok"},
            {"url": "https://x.test/b", "label": "nonsense", "reason": "bad"},
        ],
    )
    assert result["applied"] == 1
    assert len(result["errors"]) == 1


def test_skill_ships_inside_the_package():
    """`uvx --refresh` updates the server; a separately uploaded SKILL.md does not.
    Shipping it in the package is what keeps the procedure matched to the tools."""
    from pathlib import Path

    import prodscrape

    packaged = Path(prodscrape.__file__).resolve().parent / "SKILL.md"
    assert packaged.exists()
    assert "scrape-product-catalogue" in packaged.read_text(encoding="utf-8")


def test_claude_code_skill_copy_is_in_sync():
    """Two copies exist because Claude Code reads .claude/skills/ and the package ships
    its own. Drift between them is a silent failure, so it fails here instead."""
    from pathlib import Path

    import prodscrape

    root = Path(prodscrape.__file__).resolve().parents[2]
    packaged = Path(prodscrape.__file__).resolve().parent / "SKILL.md"
    code_copy = root / ".claude" / "skills" / "scrape-product-catalogue" / "SKILL.md"
    if not code_copy.exists():
        return  # not a source checkout
    assert code_copy.read_text(encoding="utf-8") == packaged.read_text(encoding="utf-8"), (
        "SKILL.md copies have drifted — copy src/prodscrape/SKILL.md over "
        ".claude/skills/scrape-product-catalogue/SKILL.md"
    )


def test_get_procedure_returns_the_packaged_skill():
    result = mcp_server.get_procedure()
    assert "procedure" in result
    assert "Never ask for a page" in result["procedure"]


def test_device_table_returns_the_devices_csv_columns():
    """The preview must match the deliverable exactly, or the agent shows a table that
    cannot be diffed against the file."""
    from prodscrape.export import CORE_COLUMNS

    result = mcp_server.device_table("binder-world.com", limit=3)
    if not result["rows"]:
        return  # no run present in this environment
    assert result["columns"] == list(CORE_COLUMNS)
    assert set(result["rows"][0]) == set(CORE_COLUMNS)
    assert result["csv_path"].endswith("devices.csv")


def test_device_table_summarises_specs_by_default():
    result = mcp_server.device_table("binder-world.com", limit=3)
    if not result["rows"]:
        return
    assert result["specs_included"] is False
    assert result["rows"][0]["specs"].endswith("keys")

    full = mcp_server.device_table("binder-world.com", limit=3, include_specs=True)
    assert full["specs_included"] is True
    assert full["rows"][0]["specs"].startswith("{")
