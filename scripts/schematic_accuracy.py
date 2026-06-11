"""Measure schematic extraction accuracy against a ground-truth JSON.

Compares a ``captify.schematic.v1`` graph (``schematic-graph.json``) to a
curated truth file (see ``scripts/make_harness_fixture.py`` for the shape):

* **Component recall / precision** by refDes (case-insensitive).
* **Part-number accuracy** on the recalled components that should have one.
* **Location accuracy** likewise (exact, case-insensitive match).
* **Net membership** — each truth net is matched to the extracted net with
  the highest Jaccard overlap of member refDes sets; reports mean Jaccard
  and how many truth nets were found exactly.

Usage:
    python scripts/schematic_accuracy.py <schematic-graph.json> <truth.json>

Exit code 0 always (it is a measurement, not a gate); print a report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return text or None


def component_metrics(graph: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    """Component metrics. ``truth["partial"] = true`` marks a SUBSET label set
    (real drawings are labeled for their significant components, not every
    passive) — recall/field accuracy are computed over the labeled set and
    precision is reported as not-applicable."""
    truth_by_ref = {_norm(c["refDes"]): c for c in truth["components"]}
    extracted_refs = { _norm(c.get("refDes")) for c in graph.get("components") or [] } - {None}

    recalled = {ref: truth_by_ref[ref] for ref in truth_by_ref if ref in extracted_refs}
    extracted_by_ref = {
        _norm(c.get("refDes")): c for c in graph.get("components") or [] if c.get("refDes")
    }

    part_expected = [c for c in recalled.values() if c.get("partNumber")]
    part_correct = sum(
        1
        for c in part_expected
        if _norm(extracted_by_ref[_norm(c["refDes"])].get("partNumber")) == _norm(c["partNumber"])
    )
    loc_expected = [c for c in recalled.values() if c.get("location")]
    loc_correct = sum(
        1
        for c in loc_expected
        if _norm(extracted_by_ref[_norm(c["refDes"])].get("location")) == _norm(c["location"])
    )

    return {
        "truth": len(truth_by_ref),
        "extracted": len(extracted_refs),
        "recalled": len(recalled),
        "recall": len(recalled) / len(truth_by_ref) if truth_by_ref else 1.0,
        "precision": len(recalled) / len(extracted_refs) if extracted_refs else 1.0,
        "partNumber": {"expected": len(part_expected), "correct": part_correct},
        "location": {"expected": len(loc_expected), "correct": loc_correct},
    }


def net_metrics(graph: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    # Map extracted net nodes (component ids) back to refDes.
    ref_by_id = {
        c["id"]: _norm(c.get("refDes"))
        for c in graph.get("components") or []
        if c.get("id")
    }
    extracted_nets = []
    for net in graph.get("nets") or []:
        members = {
            ref_by_id.get(node.get("component")) or _norm(node.get("component"))
            for node in net.get("nodes") or []
        } - {None}
        if members:
            extracted_nets.append({"name": _norm(net.get("name")), "members": members})

    rows = []
    for truth_net in truth["nets"]:
        want = {_norm(m) for m in truth_net["members"]}
        best_jaccard, best = 0.0, None
        for candidate in extracted_nets:
            union = want | candidate["members"]
            jaccard = len(want & candidate["members"]) / len(union) if union else 0.0
            if jaccard > best_jaccard:
                best_jaccard, best = jaccard, candidate
        rows.append(
            {
                "net": truth_net["name"],
                "jaccard": round(best_jaccard, 3),
                "exact": best_jaccard == 1.0,
                "nameMatched": bool(best and best["name"] == _norm(truth_net["name"])),
            }
        )
    exact = sum(1 for r in rows if r["exact"])
    mean = sum(r["jaccard"] for r in rows) / len(rows) if rows else 1.0
    return {"perNet": rows, "exact": exact, "total": len(rows), "meanJaccard": round(mean, 3)}


def main() -> int:
    graph = json.loads(Path(sys.argv[1]).read_text())
    truth = json.loads(Path(sys.argv[2]).read_text())

    comp = component_metrics(graph, truth)
    nets = net_metrics(graph, truth)
    partial = bool(truth.get("partial"))

    print("== Component extraction ==" + ("  (partial truth: labeled subset)" if partial else ""))
    if partial:
        print(f"recall {comp['recalled']}/{comp['truth']} ({comp['recall']:.0%}) | precision n/a (subset labels)")
    else:
        print(
            f"recall {comp['recalled']}/{comp['truth']} ({comp['recall']:.0%}) | "
            f"precision {comp['recalled']}/{comp['extracted']} "
            f"({(comp['recalled'] / comp['extracted']) if comp['extracted'] else 1:.0%})"
        )
    print(
        f"partNumber {comp['partNumber']['correct']}/{comp['partNumber']['expected']} | "
        f"location {comp['location']['correct']}/{comp['location']['expected']}"
    )
    print("== Net membership ==")
    for row in nets["perNet"]:
        print(
            f"  {row['net']:>8}: jaccard {row['jaccard']:.2f}"
            f"{'  exact' if row['exact'] else ''}{'  name✓' if row['nameMatched'] else ''}"
        )
    print(f"exact nets {nets['exact']}/{nets['total']} | mean jaccard {nets['meanJaccard']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
