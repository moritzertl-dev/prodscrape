"""Turn a raw spec string into a structured value. Deterministic, no model calls.

Every spec keeps its ``raw`` text alongside the parsed form (PIPELINE.md §1, Stage 3), so
a parse that gets it wrong is visible and recoverable rather than silently lossy.

Shapes seen in real vendor tables:

    "30...70 °C"                    range
    "7.5 - 10.5 L/min"              range
    "CeO+/Ce+ < 2 %"                comparison
    "660 mm x 589 mm x 1131 mm"     dimensions
    "0,25 kW"                       number with a German decimal comma
    "50/60 Hz"                      number set
    "Standard" / "yes" / "-"        text / boolean / empty
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

# Units are matched loosely: vendors write "L/min", "kcps/ppb", "µs", "° C", "l".
_UNIT = r"[°µμ]?[A-Za-zΩ%°/·\.\s]{0,14}"
# Any length: an earlier `\d{1,3}` cap silently truncated "1131 mm" to 113, which is the
# worst kind of bug here — the output stays plausible and the error is invisible.
# Separators are resolved later by parse_number, which handles the German decimal comma.
_NUM = r"[-+]?\d+(?:[.,]\d+)*"

# A trailing parenthetical qualifier is common ("200 to 3,000 rpm (max. load 300 g)") and
# must not defeat the parse. Anything else after the unit keeps the value as text rather
# than risking a half-read number.
_RANGE_RE = re.compile(
    rf"^\s*({_NUM})\s*(?:\.{{2,3}}|-|–|—|to|bis)\s*({_NUM})\s*({_UNIT}?)\s*(?:\(.*\))?\s*$",
    re.I,
)
_COMPARISON_RE = re.compile(rf"(<=|>=|<|>|≤|≥)\s*({_NUM})\s*({_UNIT})")
_DIMENSION_RE = re.compile(
    rf"({_NUM})\s*({_UNIT}?)\s*[x×]\s*({_NUM})\s*({_UNIT}?)\s*[x×]\s*({_NUM})\s*({_UNIT})",
    re.I,
)
_SIMPLE_RE = re.compile(rf"^\s*({_NUM})\s*({_UNIT})$")
_SET_RE = re.compile(rf"^\s*({_NUM})(?:\s*/\s*({_NUM}))+\s*({_UNIT})$")

BOOLEAN_TRUE = {"yes", "ja", "standard", "included", "available", "✓", "x"}
BOOLEAN_FALSE = {"no", "nein", "none", "not available", "n/a", "na", "-", "--", "—"}


def parse_number(text: str) -> float | None:
    """Parse a numeric literal, handling the German decimal comma.

    ``"0,25"`` is 0.25 while ``"1,234"`` is ambiguous; a comma followed by exactly three
    digits with no other separator is treated as a thousands separator.
    """
    text = text.strip()
    if not text:
        return None
    if "." in text and "," in text:
        # Whichever appears last is the decimal separator.
        text = (
            text.replace(".", "").replace(",", ".")
            if text.rfind(",") > text.rfind(".")
            else text.replace(",", "")
        )
    elif "," in text:
        frac = text.rsplit(",", 1)[1]
        text = text.replace(",", "") if len(frac) == 3 else text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _clean_unit(unit: str) -> str | None:
    unit = re.sub(r"\s+", " ", (unit or "").strip(" .·"))
    return unit or None


@dataclass
class SpecValue:
    """One specification, parsed but never detached from its source text."""

    raw: str
    kind: str = "text"          # number | range | comparison | dimensions | boolean | text
    value: float | None = None
    value_min: float | None = None
    value_max: float | None = None
    unit: str | None = None
    comparator: str | None = None
    values: list[float] = field(default_factory=list)
    text: str | None = None
    # Where the value came from when it is not the product page itself, e.g.
    # "datasheet:https://.../spark-datasheet.pdf". Absent means the page.
    source: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}


def parse_spec_value(raw: str) -> SpecValue:
    """Best-effort structured parse. Unrecognised input stays as text, never guessed."""
    raw = re.sub(r"\s+", " ", (raw or "").strip())
    if not raw:
        return SpecValue(raw="", kind="text", text="")

    lowered = raw.lower().strip(" .")
    if lowered in BOOLEAN_TRUE:
        return SpecValue(raw=raw, kind="boolean", value=1.0, text=raw)
    if lowered in BOOLEAN_FALSE:
        return SpecValue(raw=raw, kind="boolean", value=0.0, text=raw)

    m = _DIMENSION_RE.search(raw)
    if m:
        nums = [parse_number(m.group(i)) for i in (1, 3, 5)]
        if all(n is not None for n in nums):
            return SpecValue(
                raw=raw,
                kind="dimensions",
                values=[n for n in nums if n is not None],
                unit=_clean_unit(m.group(6) or m.group(4) or m.group(2)),
            )

    m = _RANGE_RE.match(raw)
    if m:
        lo, hi = parse_number(m.group(1)), parse_number(m.group(2))
        if lo is not None and hi is not None:
            return SpecValue(
                raw=raw, kind="range", value_min=lo, value_max=hi,
                unit=_clean_unit(m.group(3)),
            )

    m = _SET_RE.match(raw)
    if m:
        nums = [parse_number(x) for x in re.findall(_NUM, raw)]
        vals = [n for n in nums if n is not None]
        if vals:
            return SpecValue(
                raw=raw, kind="number", values=vals, value=vals[0],
                unit=_clean_unit(m.group(3)),
            )

    m = _SIMPLE_RE.match(raw)
    if m:
        val = parse_number(m.group(1))
        if val is not None:
            return SpecValue(raw=raw, kind="number", value=val, unit=_clean_unit(m.group(2)))

    m = _COMPARISON_RE.search(raw)
    if m:
        val = parse_number(m.group(2))
        if val is not None:
            return SpecValue(
                raw=raw, kind="comparison", comparator=m.group(1), value=val,
                unit=_clean_unit(m.group(3)), text=raw,
            )

    return SpecValue(raw=raw, kind="text", text=raw)


_ATTR_KEY_RE = re.compile(r"[^a-z0-9]+")


def attribute_key(name: str) -> str:
    """Stable snake_case key for an attribute label, for the EAV master table."""
    return _ATTR_KEY_RE.sub("_", name.strip().lower()).strip("_")
