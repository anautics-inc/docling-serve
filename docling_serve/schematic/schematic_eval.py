"""Score an extracted schematic graph against a ground-truth label.

The production question a scored eval answers that unit tests cannot: "did we
read THIS drawing correctly, and how much does the answer wobble run to run?"
The scoring here is pure and deterministic — a graph dict plus a golden label
dict in, a structured :class:`EvalScore` out — so it drives three things from
one definition: the offline eval runner (``tests/schematic_eval``), a CI
regression gate, and the live confidence gate that decides whether an
extraction is trustworthy enough to publish without human review.

A golden label (see ``tests/schematic_eval/corpus/*.json``) is deliberately
tolerant about counts the drawing itself renders ambiguously (how many ground
glyphs) and STRICT about the failure modes that poison downstream artifacts:

* ``forbiddenFamilies`` — component families that must not appear (or must
  stay under a cap). ``{"capacitor": 0}`` encodes "this drawing has no
  capacitors; any is a phantom." A breach zeroes the component score: an
  invented part is not a rounding error.
* ``namedNets`` + ``minNamedNetCoverage`` — the rails the drawing labels must
  survive extraction (polarity included).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Normalize a component "type" to a coarse family for count comparison. The
#: model's exact type string varies ("Power Supply" vs "power supply /
#: transformer"); families are what a label can reasonably assert.
#: First matching token wins, so more specific / composite types are listed
#: before their substrings ("power supply" before "transformer", since PS1 is
#: typed "Power Supply / transformer" and belongs to the power-supply family).
_FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("power supply", "power supply"),
    ("resistor", "resistor"),
    ("capacitor", "capacitor"),
    ("inductor", "inductor"),
    ("transformer", "transformer"),
    ("transistor", "transistor"),
    ("diode", "diode"),
    ("led", "led"),
    ("relay", "relay"),
    ("switch", "switch"),
    ("connector", "connector"),
    ("ground", "ground"),
    ("off-page", "off-page"),
    ("tube", "tube"),
    ("microcontroller", "ic"),
    ("ic", "ic"),
    ("buzzer", "buzzer"),
)


def component_family(component: dict[str, Any]) -> str:
    """Coarse family for one component ("Power Supply" -> power supply)."""
    ctype = str(component.get("type") or "").lower()
    for token, family in _FAMILY_PATTERNS:
        if token in ctype:
            return family
    return ctype.strip() or "unknown"


def _net_names(graph: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for net in graph.get("nets") or []:
        if isinstance(net, dict) and net.get("name"):
            names.add(_normalize_net(str(net["name"])))
    return names


def _normalize_net(name: str) -> str:
    """Case/space-insensitive net key that PRESERVES polarity and digits."""
    return re.sub(r"\s+", " ", name.strip().upper())


#: Descriptor abbreviations that name the SAME thing — canonicalized so
#: "OUT"/"OUTPUT" match while polarity/voltage tokens stay untouched.
_NET_TOKEN_CANON = {
    "OUT": "OUTPUT",
    "IN": "INPUT",
    "RTN": "RETURN",
    "GND": "GROUND",
    "EXC": "EXCITATION",
    "HZ": "HZ",
}


def _net_tokens(name: str) -> frozenset[str]:
    """Significant tokens of a net name (polarity kept as +/- prefixes)."""
    return frozenset(
        _NET_TOKEN_CANON.get(t, t) for t in _normalize_net(name).split(" ") if t
    )


def _net_present(expected: str, present: set[str]) -> bool:
    """Is ``expected`` covered by some extracted net name?

    Trailing descriptor words vary run to run ("230 VAC 400 Hz INPUT" vs
    "230 VAC 400 Hz"; "+13 VDC OUTPUT" vs "+13 VDC OUT"), so a match is token
    containment in EITHER direction — while polarity/voltage tokens ("+13",
    "-13") stay distinct, so a signed rail can never match its opposite.
    """
    want = _net_tokens(expected)
    if not want:
        return False
    for candidate in present:
        got = _net_tokens(candidate)
        if want <= got or got <= want:
            return True
    return False


@dataclass
class EvalScore:
    """Structured verdict for one graph against its golden label."""

    drawing: str
    componentScore: float
    netNameCoverage: float
    verifiedFraction: float | None
    phantomBreaches: list[str] = field(default_factory=list)
    familyDiffs: dict[str, dict[str, int]] = field(default_factory=dict)
    missingNets: list[str] = field(default_factory=list)
    overall: float = 0.0
    passed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "drawing": self.drawing,
            "componentScore": round(self.componentScore, 3),
            "netNameCoverage": round(self.netNameCoverage, 3),
            "verifiedFraction": self.verifiedFraction,
            "phantomBreaches": self.phantomBreaches,
            "familyDiffs": self.familyDiffs,
            "missingNets": self.missingNets,
            "overall": round(self.overall, 3),
            "passed": self.passed,
        }


def score_graph(graph: dict[str, Any], label: dict[str, Any]) -> EvalScore:
    """Score ``graph`` against a golden ``label``.

    ``label`` keys: ``drawing``, ``componentsByFamily`` (expected counts),
    ``componentTolerance`` (allowed +/- per family, default 0), ``forbidden
    Families`` (family -> max allowed), ``namedNets`` (list), ``minNamedNet
    Coverage``, ``minVerifiedFraction``, ``minOverall``.
    """
    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    actual: dict[str, int] = {}
    for component in components:
        family = component_family(component)
        actual[family] = actual.get(family, 0) + 1

    expected: dict[str, int] = label.get("componentsByFamily") or {}
    tolerance: dict[str, int] = label.get("componentTolerance") or {}

    # Per-family count error beyond the drawing's declared ambiguity tolerance.
    family_diffs: dict[str, dict[str, int]] = {}
    total_expected = 0
    total_error = 0
    for family in set(expected) | set(actual):
        exp = int(expected.get(family, 0))
        act = int(actual.get(family, 0))
        tol = int(tolerance.get(family, 0))
        error = max(0, abs(act - exp) - tol)
        total_expected += exp
        total_error += error
        if error:
            family_diffs[family] = {"expected": exp, "actual": act, "tolerance": tol}
    component_score = (
        max(0.0, 1.0 - total_error / total_expected) if total_expected else 1.0
    )

    # Phantom guard: forbidden families are hard failures.
    phantom_breaches: list[str] = []
    for family, cap in (label.get("forbiddenFamilies") or {}).items():
        act = int(actual.get(family, 0))
        if act > int(cap):
            phantom_breaches.append(f"{family}: {act} > allowed {cap}")
    if phantom_breaches:
        component_score = 0.0

    # Named-net survival: token-containment match (descriptor words wobble)
    # but polarity/voltage tokens stay distinct.
    expected_nets = list(label.get("namedNets") or [])
    present = _net_names(graph)
    missing = sorted(n for n in expected_nets if not _net_present(n, present))
    net_coverage = (
        (len(expected_nets) - len(missing)) / len(expected_nets)
        if expected_nets
        else 1.0
    )

    quality = graph.get("connectivityQuality") or {}
    verified = quality.get("verifiedComponentFraction")

    overall = (
        0.5 * component_score
        + 0.3 * net_coverage
        + 0.2 * (component_score if not phantom_breaches else 0.0)
    )

    passed = (
        not phantom_breaches
        and component_score >= label.get("minComponentScore", 0.7)
        and net_coverage >= label.get("minNamedNetCoverage", 0.8)
        and overall >= label.get("minOverall", 0.7)
    )
    if label.get("minVerifiedFraction") is not None and isinstance(
        verified, (int, float)
    ):
        passed = passed and verified >= label["minVerifiedFraction"]

    return EvalScore(
        drawing=str(
            label.get("drawing") or graph.get("source", {}).get("fileName") or "?"
        ),
        componentScore=component_score,
        netNameCoverage=net_coverage,
        verifiedFraction=verified,
        phantomBreaches=phantom_breaches,
        familyDiffs=family_diffs,
        missingNets=missing,
        overall=overall,
        passed=passed,
    )


def aggregate_runs(scores: list[EvalScore]) -> dict[str, Any]:
    """Mean/min/max + pass-rate across N runs of ONE drawing (variance)."""
    if not scores:
        return {}
    overalls = [s.overall for s in scores]
    comps = [s.componentScore for s in scores]
    n = len(scores)
    mean = sum(overalls) / n
    return {
        "runs": n,
        "overallMean": round(mean, 3),
        "overallMin": round(min(overalls), 3),
        "overallMax": round(max(overalls), 3),
        "overallSpread": round(max(overalls) - min(overalls), 3),
        "componentScoreMean": round(sum(comps) / n, 3),
        "passRate": round(sum(1 for s in scores if s.passed) / n, 3),
        "anyPhantom": any(s.phantomBreaches for s in scores),
    }
