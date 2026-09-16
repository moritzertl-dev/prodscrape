"""Stage 4 — export.

The master is EAV (long): lossless, sparse-friendly, and able to hold devices whose spec
sets have nothing in common. Everything else is a *view* over it, so adding a category
profile later never requires re-scraping (PIPELINE.md §0).

The wide table is the shape asked for: a handful of core columns every device has, plus a
`specs` JSON bag a downstream agent or RAG can read for anything category-specific.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .extract import DeviceRecord

# The device table carries information *about devices* only. Counters describing how the
# scrape went (spec_count, variants_merged, spec_source, warnings) are provenance: they
# live in extracted.jsonl and the manifest, not in the deliverable table.
CORE_COLUMNS = (
    "product_id",
    "manufacturer",
    "name",
    "category",
    "url",
    "interfaces",
    "description",
    "image_url",
    "datasheet_urls",
    "specs",
)


def is_device_row(record: DeviceRecord) -> bool:
    """Whether a record belongs in the device table.

    A record with no specifications came from a page where no table passed validation —
    in practice a series or overview page ("PQ LC Series", "AOX Autosampler Series"), not
    a device. Those go to the review queue instead of polluting the table.
    """
    return bool(record.specs)


def to_eav(records: list[DeviceRecord]) -> list[dict]:
    """The lossless master: one row per (device, attribute)."""
    rows: list[dict] = []
    for rec in records:
        for key, spec in rec.specs.items():
            rows.append(
                {
                    "product_id": rec.product_id,
                    "manufacturer": rec.manufacturer,
                    "device": rec.name,
                    "attribute": key,
                    "kind": spec.kind,
                    "value": spec.value if spec.value is not None else "",
                    "value_min": spec.value_min if spec.value_min is not None else "",
                    "value_max": spec.value_max if spec.value_max is not None else "",
                    "unit": spec.unit or "",
                    "raw": spec.raw,
                    "source_url": rec.url,
                }
            )
    return rows


def to_wide(records: list[DeviceRecord]) -> list[dict]:
    """Core columns plus a `specs` JSON bag — the deliverable table."""
    rows = []
    for rec in records:
        rows.append(
            {
                "product_id": rec.product_id,
                "manufacturer": rec.manufacturer,
                "name": rec.name,
                "category": rec.category,
                "url": rec.url,
                "interfaces": "; ".join(rec.interfaces),
                "description": rec.description[:300],
                "image_url": rec.image_url,
                "datasheet_urls": "; ".join(rec.datasheet_urls[:5]),
                "specs": json.dumps(
                    {k: v.raw for k, v in rec.specs.items()}, ensure_ascii=False
                ),
            }
        )
    return rows


def to_review_queue(records: list[DeviceRecord]) -> list[dict]:
    """Rows that need a judgment call before they can join the table.

    Having no spec table is a strong indicator of a series or overview page, but it is an
    indicator and not proof — a genuine device could be documented without a table. These
    are therefore held for review rather than discarded, with the evidence needed to
    decide attached.
    """
    rows = []
    for rec in records:
        rows.append(
            {
                "product_id": rec.product_id,
                "name": rec.name,
                "category": rec.category,
                "url": rec.url,
                "description": rec.description[:300],
                "interfaces": "; ".join(rec.interfaces),
                "datasheet_count": len(rec.datasheet_urls),
                "reason": "; ".join(rec.warnings) or "no specifications extracted",
                "verdict": "",       # device | not-a-device — filled by review
                "verdict_reason": "",
            }
        )
    return rows


def write_csv(rows: list[dict], path: Path, columns: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    columns = columns or list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_jsonl(records: list[DeviceRecord], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def pivot_category(records: list[DeviceRecord], category: str, min_coverage: float = 0.5):
    """Promote frequently-seen attributes of one category into real columns.

    This is the "category profile" as a *view*: it reads the same extracted data and
    needs no re-scrape. An attribute becomes a column when enough devices in the category
    actually report it.
    """
    members = [r for r in records if r.category == category]
    if not members:
        return [], []
    counts: dict[str, int] = {}
    for rec in members:
        for key in rec.specs:
            counts[key] = counts.get(key, 0) + 1
    threshold = max(1, int(len(members) * min_coverage))
    columns = sorted(k for k, n in counts.items() if n >= threshold)

    rows = []
    for rec in members:
        row = {"product_id": rec.product_id, "name": rec.name, "url": rec.url}
        for key in columns:
            spec = rec.specs.get(key)
            row[key] = spec.raw if spec else ""
        rows.append(row)
    return rows, ["product_id", "name", "url", *columns]
