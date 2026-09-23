"""Batched model judgments over compact digests — never over pages.

Two questions, each asked only about the minority structure could not settle:

* ``classify_pages`` — tier B of classification: what is this page?
* ``review_records`` — a record with no parsed specs: device, or overview page?

Both write their verdicts to the same ``VerdictStore`` the agent path uses, with
``decided_by="model"``, so a verdict is identical in shape whoever gave it and is never
asked twice. The prompts are also served to a driving agent by the MCP tools, so both
paths answer the same question in the same words.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urlparse

from .llm import BudgetExceeded, Reasoner
from .verdicts import VALID_LABELS, VerdictStore

BATCH = 25

CLASSIFY_SYSTEM = """\
You label pages of a manufacturer's website from compact digests. Labels:
instrument  - a page for a physical device or device family the vendor sells
              (analyzer, reader, robot, liquid handler, workstation, centrifuge,
              incubator, storage system, integrated system, device module)
accessory   - add-on hardware for an instrument (rotor, head, stacker, adapter, module
              not sold as a device on its own)
consumable  - tips, plates, tubes, reagents, kits, columns, labware
software    - software-only product
service     - services, support, training, contracts
category_page - a page listing or introducing several products
other       - applications, workflows, industries, news, events, company, literature
Reply with one JSON object: {"verdicts": [{"i": <index>, "label": "...", "reason": "<=12 words"}]}
Judge from the evidence given; the menu path the page was reached through ("via") is
strong evidence of what the vendor considers it."""

REVIEW_SYSTEM = """\
Each record is a product page from which no specification table could be parsed.
Decide whether it describes a DEVICE the vendor sells (physical instrument or instrument
family) or NOT a device (overview/series landing page, application page, accessory,
consumable, software, service). Reply with one JSON object:
{"verdicts": [{"i": <index>, "verdict": "device" | "not-a-device", "reason": "<=12 words"}]}"""


def _compact(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def classify_pages(
    reasoner: Reasoner, digests: list[dict], store: VerdictStore
) -> dict:
    """Label digests in batches; returns counts. Stops cleanly at the budget."""
    applied, failed, stopped = 0, 0, ""
    for start in range(0, len(digests), BATCH):
        batch = digests[start:start + BATCH]
        body = "\n".join(_compact({"i": i, **d}) for i, d in enumerate(batch))
        try:
            reply = reasoner.ask_json("classify", CLASSIFY_SYSTEM, body, items=len(batch))
        except BudgetExceeded as exc:
            stopped = str(exc)
            break
        except (ValueError, RuntimeError) as exc:
            failed += len(batch)
            stopped = f"batch failed: {exc}"
            continue
        for v in reply.get("verdicts", []):
            try:
                digest = batch[int(v["i"])]
                label = str(v["label"]).strip()
                if label not in VALID_LABELS:
                    continue
                store.set_classification(digest["url"], label, str(v.get("reason", "")))
                applied += 1
            except (KeyError, ValueError, IndexError, TypeError):
                failed += 1
        store.save()
    return {"judged": applied, "failed": failed, "stopped": stopped}


def review_records(
    reasoner: Reasoner, rows: list[dict], store: VerdictStore
) -> dict:
    applied, failed, stopped = 0, 0, ""
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        body = "\n".join(_compact({"i": i, **{k: v for k, v in r.items()
                                            if k != "product_id"}})
                         for i, r in enumerate(batch))
        try:
            reply = reasoner.ask_json("review", REVIEW_SYSTEM, body, items=len(batch))
        except BudgetExceeded as exc:
            stopped = str(exc)
            break
        except (ValueError, RuntimeError) as exc:
            failed += len(batch)
            stopped = f"batch failed: {exc}"
            continue
        for v in reply.get("verdicts", []):
            try:
                row = batch[int(v["i"])]
                store.set_review(row["product_id"], str(v["verdict"]).strip(),
                                 str(v.get("reason", "")))
                applied += 1
            except (KeyError, ValueError, IndexError, TypeError):
                failed += 1
        store.save()
    return {"judged": applied, "failed": failed, "stopped": stopped}


TRIAGE_BATCH = 150
TRIAGE_SYSTEM = """\
You filter links found on a manufacturer's catalogue pages, before any of them is
fetched. Keep a link when it probably leads to a page about an INSTRUMENT the vendor
sells (a physical device or device family), or to a listing page of instruments.
Drop links to consumables, reagents, kits, labware, software-only products, services,
applications and workflows, literature, citations, documents, news, events, support,
company pages and forms. When unsure, keep it — a kept link costs one page fetch, a
dropped instrument is lost.
Each line is: id | link text | host/path | found on page.
Reply with one JSON object: {"keep": [ids]}"""


def triage_links(reasoner: Reasoner, cands: list, known: dict[str, bool]) -> list[bool]:
    """Which harvested links are worth fetching. Decisions persist in ``known``.

    This is the cheapest judgment in the pipeline and among the most valuable: ~15 tokens
    per link instead of a page fetch plus a ~250-token digest. On Tecan, hub pages linked
    700 URLs, most of them citations, application notes and journal articles.
    """
    out: list[bool | None] = [known.get(c.url) for c in cands]
    todo = [i for i, v in enumerate(out) if v is None]
    for start in range(0, len(todo), TRIAGE_BATCH):
        chunk = todo[start:start + TRIAGE_BATCH]
        lines = []
        for j, i in enumerate(chunk):
            c = cands[i]
            u = urlparse(c.url)
            slug = u.path.rstrip("/").rsplit("/", 1)[-1]
            # The name is often the slug in words; send it once.
            name = "" if _same_words(c.name, slug) else c.name[:70]
            lines.append(f"{j} | {name} | {u.path[-80:]} | {c.category[:40]}")
        try:
            reply = reasoner.ask_json("triage", TRIAGE_SYSTEM, "\n".join(lines),
                                      items=len(chunk))
            kept = {int(x) for x in reply.get("keep", []) if str(x).lstrip("-").isdigit()}
        except (BudgetExceeded, ValueError, RuntimeError):
            kept = set(range(len(chunk)))          # fail open: fetch rather than lose
        for j, i in enumerate(chunk):
            out[i] = j in kept
            known[cands[i].url] = j in kept
    return [bool(v) for v in out]


def _same_words(a: str, b: str) -> bool:
    words = lambda t: re.findall(r"[a-z0-9]+", t.lower())
    return words(a) == words(b)


BRANCH_SYSTEM = """\
You filter branches of a manufacturer's URL tree before any page in them is fetched.
Each line is: id | branch path | number of pages | sample page slugs.
Keep a branch when its pages are probably INSTRUMENTS (physical devices or device
families) or may include some. Drop branches that are clearly consumables, columns,
reagents, kits, labware, spare parts, software, services, literature or support.
When unsure, keep it.
Reply with one JSON object: {"keep": [ids]}"""


def triage_branches(reasoner: Reasoner, cands: list, known: dict[str, bool],
                    *, batch: int = 200) -> list:
    """Filter a large candidate set by URL-tree branch, then return the survivors.

    agilent.com has ~4,000 leaf pages under its catalogue branches, most of them
    columns, vials and spare parts. Asking about each URL cost ~$0.11 per 150; asking
    about the ~300 parent branches, with page counts and sample slugs, costs a
    fraction and drops "gc-columns/..." wholesale.
    """
    groups: dict[str, list] = defaultdict(list)
    for c in cands:
        path = urlparse(c.url).path.rstrip("/")
        groups[path.rsplit("/", 1)[0] + "/"].append(c)
    branches = sorted(groups)
    todo = [b for b in branches if f"branch:{b}" not in known]
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        lines = []
        for j, b in enumerate(chunk):
            slugs = [urlparse(c.url).path.rstrip("/").rsplit("/", 1)[-1][:40]
                     for c in groups[b][:3]]
            lines.append(f"{j} | {b[-90:]} | {len(groups[b])} | {', '.join(slugs)}")
        try:
            reply = reasoner.ask_json("triage", BRANCH_SYSTEM, "\n".join(lines),
                                      items=len(chunk))
            kept = {int(x) for x in reply.get("keep", []) if str(x).lstrip("-").isdigit()}
        except (BudgetExceeded, ValueError, RuntimeError):
            kept = set(range(len(chunk)))
        for j, b in enumerate(chunk):
            known[f"branch:{b}"] = j in kept
    return [c for b in branches if known.get(f"branch:{b}", True) for c in groups[b]]


RELEVANCE_SYSTEM = """\
You screen a manufacturer's device list for a laboratory-automation company. Answer two
questions per row.

1. single: is the row ONE device — a specific named product or model series a customer
   could order (e.g. "Spark", "Avanti JXN-26 Series", "Cavro XLP 6000 Pump")?
   Answer false for overview pages: a category or range ("Liquid handling components",
   "Microplate readers"), a technology, capability or application page ("Live cell
   imaging"), a portfolio, solution or bundle description, or a generic name.
   Answer true for a module or option only when it is sold as its own device.
   Also true: a specific product whose name happens to be plain ("Reagent Exchanger" —
   an automated plate washer), a named model series ("Avanti JXN-26 Series"), and one
   model presented in two variants on one page ("Allegra X-30 and X-30R").
   False also for: bundles of several different instruments, upgrade keys, rows named
   after table labels ("... Model", "... PN"), and generic technique names ("Purge and
   Trap").
2. relevance: could the device be part of an AUTOMATED LABORATORY in any way —
   integrated into a workcell, loaded by a robot, controlled or read out by software,
   connected to a LIMS or scheduler?
   yes   - clearly automatable or an automation device
   maybe - plausible, e.g. a benchtop instrument with a data interface
   no    - clearly impossible: manual hand tools, purely manual apparatus, passive parts
           (rotors, adapters, cables, kits, upgrade licences), consumables, software-only
   Be lenient on relevance: when in doubt, maybe. Be strict on single.
Reply with one JSON object: {"verdicts": [{"i": <index>, "single": true|false,
"relevance": "yes"|"maybe"|"no", "reason": "<=12 words"}]}"""


def relevance_rows(records: list, store: VerdictStore) -> list[dict]:
    """Compact rows for devices not yet screened (~80 tokens each)."""
    out = []
    for r in records:
        if store.relevance_for(r["product_id"]) is not None:
            continue
        out.append({
            "product_id": r["product_id"],
            "name": r["name"][:90],
            "category": (r.get("category") or "")[:50],
            "interfaces": r.get("interfaces", []),
            "description": (r.get("description") or "")[:200],
            "spec_keys": list(r.get("specs", {}))[:8],
        })
    return out


def screen_relevance(reasoner: Reasoner, rows: list[dict], store: VerdictStore,
                     *, batch: int = 40) -> dict:
    applied, failed, stopped, removed = 0, 0, "", 0
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        body = "\n".join(_compact({"i": i, **{k: v for k, v in r.items()
                                             if k != "product_id"}})
                         for i, r in enumerate(chunk))
        try:
            reply = reasoner.ask_json("relevance", RELEVANCE_SYSTEM, body, items=len(chunk))
        except BudgetExceeded as exc:
            stopped = str(exc)
            break
        except (ValueError, RuntimeError) as exc:
            failed += len(chunk)
            stopped = f"batch failed: {exc}"
            continue
        for v in reply.get("verdicts", []):
            try:
                row = chunk[int(v["i"])]
                verdict = str(v["relevance"]).strip().lower()
                reason = str(v.get("reason", ""))
                # Only single devices belong in the table: an overview page is
                # excluded whatever its automation relevance.
                if v.get("single") is False:
                    verdict, reason = "no", f"not a single device: {reason}"
                store.set_relevance(row["product_id"], verdict, reason)
                applied += 1
                removed += verdict == "no"
            except (KeyError, ValueError, IndexError, TypeError):
                failed += 1
        store.save()
    return {"judged": applied, "failed": failed, "excluded": removed, "stopped": stopped}
