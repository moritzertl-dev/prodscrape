"""Device identity — deciding what earns its own row.

The rule (agreed 2026-09-16): **one row per device. Only a functional hardware difference
creates a separate device.** Two listings collapse into one when every differing attribute
is non-functional — mains voltage, power frequency, plug type, regional approval, article
number, bundled software, warranty.

Consequences worth stating, because they are the cases that come up:

- a 120 V US unit and a 230 V EU unit of the same machine are **one device**
- an added hardware module (BINDER ``KB PRO 260 with ICH light module``) is **separate**
- a different orbit diameter (``BioShake Q1`` vs ``Q1 3.0 mm``) is **separate**
- identical hardware with a different software bundle or warranty is **one device**

Grounded in real data rather than assumption:

- BINDER ``B028-230V`` vs ``B028-120V`` share a spec table where 26 attributes are listed
  and exactly 4 differ: Article Number, Designation, Rated Voltage, Power frequency. Every
  functional spec — temperature range, uniformity, interior volume, dimensions, shelves —
  is identical. **One device.**
- QInstruments ``BioShake 3000`` vs ``BioShake 3000 elm`` differ by an exchangeable
  magnetic lock, and all BioShakes run on a universal 85-264 VAC supply, so no regional
  split exists at all. **Separate devices.**
- analytik-jena ``PlasmaQuant MS`` / ``MS Elite`` / ``MS Elite S`` / ``MS Q`` differ in
  detector sensitivity, cones and roughing pump. **Four devices.**

Merging is always evidence-backed and reversible: the merged row keeps every source
designation, article number and differing value, plus the reason it merged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from os.path import commonprefix

# Attribute names whose differences never, on their own, create a separate device.
# Matched as substrings against a normalised (lowercased, punctuation-stripped) name.
COSMETIC_ATTRIBUTE_PATTERNS = (
    # electrical supply / region
    "rated voltage",
    "nominal voltage",
    "supply voltage",
    "mains voltage",
    "voltage",
    "power frequency",
    "mains frequency",
    "frequency hz",
    "phase nominal voltage",
    "plug",
    "power cord",
    "power cable",
    # Consequences of the mains build rather than capabilities of the hardware: a 120 V
    # unit draws double the current, so its fuse rating and 60 Hz fan noise differ while
    # the machine is identical. BINDER's BD056-230V and BD056UL-120V differ only here.
    "unit fuse",
    "fuse",
    "nominal power",
    "power consumption",
    "energy consumption",
    "connected load",
    "rated current",
    "current consumption",
    "sound pressure level",
    "sound-pressure level",
    # identifiers and market
    "article number",
    "order number",
    "catalog number",
    "catalogue number",
    "part number",
    "sku",
    "designation",
    "model code",
    "region",
    "market",
    "country",
    "certification",
    "approval",
    "ul listing",
    # packaging / cosmetics
    "colour",
    "color",
    "packaging",
    "shipping weight",
    "packing",
    # software / firmware / commercial — only *functional hardware* differences create a
    # separate device, so a different bundled software package or warranty does not.
    "software version",
    "firmware",
    "bundled software",
    "included software",
    "software package",
    "licence",
    "license",
    "warranty",
    "service contract",
    "documentation language",
    "manual language",
    "price",
    "list price",
    "delivery time",
    "availability",
)

# Never treat these as cosmetic even though they contain a cosmetic substring.
FUNCTIONAL_OVERRIDES = (
    "operating voltage range",
    "output voltage",
)

_NORMALISE_RE = re.compile(r"[^a-z0-9 ]+")
_VOLTAGE_SUFFIX_RE = re.compile(r"[-_ ]*(ul|ce|csa)?[-_ ]*\d{2,3}\s*v(ac)?$", re.I)


def normalise_attribute(name: str) -> str:
    return _NORMALISE_RE.sub(" ", name.strip().lower()).strip()


def is_cosmetic_attribute(name: str) -> bool:
    """Whether a difference in this attribute alone leaves the device unchanged."""
    norm = normalise_attribute(name)
    if not norm:
        return True
    if any(o in norm for o in FUNCTIONAL_OVERRIDES):
        return False
    return any(p in norm for p in COSMETIC_ATTRIBUTE_PATTERNS)


def differing_attributes(a: dict[str, str], b: dict[str, str]) -> list[str]:
    """Attribute names whose values differ between two variants."""
    keys = set(a) | set(b)
    return sorted(k for k in keys if (a.get(k) or "").strip() != (b.get(k) or "").strip())


@dataclass
class MergeDecision:
    """Why two variants were or were not treated as the same device."""

    merged: bool
    differing: list[str] = field(default_factory=list)
    functional: list[str] = field(default_factory=list)
    cosmetic: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if not self.differing:
            return "identical specifications"
        if self.merged:
            return (
                "differs only in non-functional attributes: "
                + ", ".join(self.cosmetic)
            )
        return "functional differences: " + ", ".join(self.functional)


# Tokens that describe the regional/electrical build rather than the hardware itself.
_REGIONAL_TOKEN_RE = re.compile(
    r"\b(?:\d{2,3}\s*v(?:ac)?|\d{2,3}\s*-\s*\d{2,3}\s*v|ul|ce|csa|\d{2}/\d{2}\s*hz|\d{2}\s*hz)\b",
    re.I,
)
_ORDER_CODE_RE = re.compile(r"\b[A-Z]{0,4}\d[\dA-Za-z]*(?:-[\dA-Za-z]+){2,}\b")


def normalise_variant_name(name: str) -> str:
    """A variant name reduced to the hardware it describes.

    Regional build tokens and order codes are removed; whatever remains must match for
    two listings to be the same device.
    """
    text = _ORDER_CODE_RE.sub(" ", name or "")
    text = _REGIONAL_TOKEN_RE.sub(" ", text)
    # An approval suffix welded onto the model code: "BD056UL" is the same hardware as
    # "BD056". Only stripped directly after digits so a real name is never truncated.
    text = re.sub(r"(?<=\d)(?:ul|ce|csa)\b", " ", text, flags=re.I)
    text = _NORMALISE_RE.sub(" ", text.lower())
    return " ".join(text.split())


def names_equivalent(a: str, b: str) -> bool:
    """Whether two variant names describe the same hardware.

    The spec table is not always complete evidence. ``"qTOWER iris touch"`` and
    ``"qTOWER iris"`` list identical specifications because the touchscreen is never a
    row in the table — the only signal that they are different devices is the name. So a
    name difference that survives regional-token stripping blocks the merge.
    """
    return normalise_variant_name(a) == normalise_variant_name(b)


def compare_variants(a: dict[str, str], b: dict[str, str]) -> MergeDecision:
    differing = differing_attributes(a, b)
    cosmetic = [k for k in differing if is_cosmetic_attribute(k)]
    functional = [k for k in differing if not is_cosmetic_attribute(k)]
    return MergeDecision(
        merged=not functional,
        differing=differing,
        functional=functional,
        cosmetic=cosmetic,
    )


def canonical_name(names: list[str]) -> str:
    """A device name for a merged group, with the regional suffix removed.

    ``["B028-230V", "B028-120V"]`` -> ``"B028"``.
    """
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return ""
    if len(names) == 1:
        return _VOLTAGE_SUFFIX_RE.sub("", names[0]).strip(" -_") or names[0]
    shared = commonprefix(names).strip(" -_/(")
    if len(shared) >= 3:
        return shared
    return _VOLTAGE_SUFFIX_RE.sub("", names[0]).strip(" -_") or names[0]


@dataclass
class Device:
    """One row of the final table."""

    name: str
    source_variants: list[str] = field(default_factory=list)
    specs: dict[str, str] = field(default_factory=dict)
    regional_variants: list[dict[str, str]] = field(default_factory=list)
    merge_reason: str = ""

    @property
    def is_merged(self) -> bool:
        return len(self.source_variants) > 1


def group_devices(records: dict[str, dict[str, str]]) -> list[Device]:
    """Collapse variants that differ only cosmetically into one device each.

    ``records`` maps a variant name to its ``{attribute: value}`` mapping, exactly as
    ``tables.to_spec_table`` produces. Comparison is pairwise against the first member of
    a group, so a group only forms when every member is cosmetically equivalent to it.
    """
    groups: list[list[str]] = []
    decisions: list[MergeDecision] = []

    for name, specs in records.items():
        placed = False
        for i, group in enumerate(groups):
            if not names_equivalent(group[0], name):
                continue
            decision = compare_variants(records[group[0]], specs)
            if decision.merged:
                group.append(name)
                decisions[i] = decision
                placed = True
                break
        if not placed:
            groups.append([name])
            decisions.append(MergeDecision(merged=False))

    devices: list[Device] = []
    for group, decision in zip(groups, decisions):
        base = dict(records[group[0]])
        differing: set[str] = set()
        for member in group[1:]:
            differing.update(differing_attributes(records[group[0]], records[member]))

        # Attributes that vary within the group move out of the shared spec set and are
        # kept per-variant, so nothing is silently dropped.
        shared = {k: v for k, v in base.items() if k not in differing}
        regional = [
            {k: records[m].get(k, "") for k in sorted(differing)} | {"_variant": m}
            for m in group
        ] if len(group) > 1 else []

        devices.append(
            Device(
                name=canonical_name(group),
                source_variants=list(group),
                specs=shared if len(group) > 1 else base,
                regional_variants=regional,
                merge_reason=decision.reason if len(group) > 1 else "single variant",
            )
        )
    return devices
