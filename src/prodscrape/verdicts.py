"""Persisted agent judgments.

The reproducibility contract (PIPELINE.md §0) says Claude's decisions are artifacts, not
transient. Every verdict the agent gives is written to ``runs/<domain>/verdicts.json`` and
re-applied on later runs, so a re-scan never re-asks a question already answered and a run
can be replayed with no model involvement at all.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

CLASSIFICATION = "classification"
REVIEW = "review"

VALID_LABELS = {
    "instrument", "accessory", "consumable", "software", "service",
    "category_page", "other",
}
VALID_VERDICTS = {"device", "not-a-device"}


@dataclass
class VerdictStore:
    """Two keyed maps: stage-2 labels by URL, stage-3.6 verdicts by product_id."""

    path: Path
    data: dict

    @classmethod
    def load(cls, run_dir: Path) -> "VerdictStore":
        path = Path(run_dir) / "verdicts.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {CLASSIFICATION: {}, REVIEW: {}}
        data.setdefault(CLASSIFICATION, {})
        data.setdefault(REVIEW, {})
        return cls(path=path, data=data)

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return self.path

    # -- stage 2 -------------------------------------------------------------
    def set_classification(self, url: str, label: str, reason: str) -> None:
        if label not in VALID_LABELS:
            raise ValueError(f"unknown label {label!r}; expected one of {sorted(VALID_LABELS)}")
        self.data[CLASSIFICATION][url] = {
            "label": label,
            "reason": reason,
            "decided_by": "model",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def classification_for(self, url: str) -> dict | None:
        return self.data[CLASSIFICATION].get(url)

    # -- stage 3.6 -----------------------------------------------------------
    def set_review(self, product_id: str, verdict: str, reason: str) -> None:
        if verdict not in VALID_VERDICTS:
            raise ValueError(
                f"unknown verdict {verdict!r}; expected one of {sorted(VALID_VERDICTS)}"
            )
        self.data[REVIEW][product_id] = {
            "verdict": verdict,
            "reason": reason,
            "decided_by": "model",
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def review_for(self, product_id: str) -> dict | None:
        return self.data[REVIEW].get(product_id)

    @property
    def counts(self) -> dict:
        return {
            "classifications": len(self.data[CLASSIFICATION]),
            "reviews": len(self.data[REVIEW]),
        }
