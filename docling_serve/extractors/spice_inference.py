"""Infer first-order SPICE physics for components without vendor models.

Model resolution in the netlist exporter is a three-tier fallback:

1. **Vendor/catalog model** (``spice_models.find_model``) — the truth.
2. **Inferred model** (this module) — first-order physics derived from the
   component's *type taxonomy* (relay → coil R+L, switch → closed contact,
   lamp → filament resistance, diode → generic junction, …). Every inferred
   body is labelled ``INFERRED`` in the netlist so an engineer can tell it
   from vendor data at a glance.
3. **Connectivity stub** — multi-pin ICs, connectors, and anything whose
   internals genuinely cannot be guessed keep the 1G-resistor stub.

Inference keys on generic electrical *type keywords only* — never on part
numbers, vendors, or drawing names — so it applies to any schematic. Printed
component values (``10k``, ``0.1uF``) are honoured when present; otherwise
type-typical defaults are used and stated in the body comment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InferredBody:
    """Internals for one inferred ``.subckt`` plus its rationale."""

    lines: tuple[str, ...]
    rationale: str
    #: Pin counts the body is valid for (None = any >= 2; uses first/last pin).
    exact_pins: int | None = None


#: (type keywords, builder) — first match wins; checked in order so more
#: specific electromechanical classes beat generic ones.
def _coil(resistance: str, inductance: str, what: str) -> InferredBody:
    return InferredBody(
        lines=(
            f"R1 p1 m {resistance}",
            f"L1 m p2 {inductance}",
        ),
        rationale=f"{what} winding as series R+L",
        exact_pins=2,
    )


def _contact(resistance: str, what: str) -> InferredBody:
    return InferredBody(
        lines=(f"R1 p1 p2 {resistance}",),
        rationale=f"{what} as closed contact",
        exact_pins=2,
    )


def _resistive(resistance: str, what: str) -> InferredBody:
    return InferredBody(
        lines=(f"R1 p1 p2 {resistance}",),
        rationale=f"{what} as equivalent resistance",
        exact_pins=2,
    )


_DIODE = InferredBody(
    lines=("D1 p1 p2 DGEN",), rationale="generic junction diode", exact_pins=2
)
_LED = InferredBody(
    lines=("D1 p1 p2 DLEDGEN",), rationale="generic LED junction", exact_pins=2
)
_CRYSTAL = InferredBody(
    lines=(
        "L1 p1 m1 10m",
        "C1 m1 m2 25f",
        "R1 m2 p2 50",
        "C0 p1 p2 5p",
    ),
    rationale="quartz resonator as series RLC with shunt capacitance",
    exact_pins=2,
)
_NPN = InferredBody(
    lines=("Q1 p1 p2 p3 QGENNPN",),
    rationale="generic NPN (nodes taken as C B E in traced order)",
    exact_pins=3,
)
_PNP = InferredBody(
    lines=("Q1 p1 p2 p3 QGENPNP",),
    rationale="generic PNP (nodes taken as C B E in traced order)",
    exact_pins=3,
)

#: ``.model`` cards required by inferred bodies, emitted once per netlist.
MODEL_CARDS: dict[str, str] = {
    "DGEN": ".model DGEN D()",
    "DLEDGEN": ".model DLEDGEN D(N=2 IS=1e-18)",
    "QGENNPN": ".model QGENNPN NPN(BF=100)",
    "QGENPNP": ".model QGENPNP PNP(BF=100)",
}

#: Ordered (keywords, body) inference table. Keywords match the lower-cased
#: component type; the FIRST row with a hit wins.
_RULES: tuple[tuple[tuple[str, ...], InferredBody], ...] = (
    (("relay", "contactor"), _coil("280", "100m", "relay coil")),
    (("solenoid", "valve"), _coil("28", "50m", "solenoid coil")),
    (("motor", "booster", "actuator", "pump", "fan"), _coil("5", "10m", "DC motor")),
    (("transformer",), _coil("10", "100m", "transformer primary")),
    (
        ("switch", "breaker", "button", "pushbutton", "interlock", "trigger"),
        _contact("10m", "switch"),
    ),
    (("fuse", "jumper", "shunt", "link"), _contact("1m", "fuse/link")),
    (("lamp", "light", "indicator", "bulb", "annunciator"), _resistive("28", "lamp filament")),
    (("heater", "thermistor", "resistor element"), _resistive("100", "heating element")),
    (("led",), _LED),
    (("zener", "diode", "rectifier"), _DIODE),
    (("crystal", "resonator", "oscillator element"), _CRYSTAL),
    (("npn",), _NPN),
    (("pnp",), _PNP),
    (("transistor",), _NPN),
)

_VALUE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(k|meg|m|u|n|p|f|g)?", re.IGNORECASE)


def infer_subckt_body(
    component: dict[str, Any], pin_count: int
) -> InferredBody | None:
    """First-order physics for one component, or ``None`` (keep the stub).

    Matching uses the component ``type`` transcribed from the drawing.
    2-terminal bodies apply only to 2-pin instances (with >2 traced pins the
    terminal pairing is unknowable, so inference would silently mis-wire);
    transistor bodies require exactly 3.
    """
    ctype = str(component.get("type") or "").lower()
    if not ctype:
        return None
    for keywords, body in _RULES:
        if any(k in ctype for k in keywords):
            if body.exact_pins is not None and pin_count != body.exact_pins:
                return None
            return _apply_printed_value(component, body)
    return None


def model_cards_for(bodies: list[InferredBody]) -> list[str]:
    """The ``.model`` cards the given inferred bodies depend on (deduped)."""
    needed: list[str] = []
    for body in bodies:
        for line in body.lines:
            for model_name, card in MODEL_CARDS.items():
                if line.endswith(model_name) and card not in needed:
                    needed.append(card)
    return needed


def _apply_printed_value(
    component: dict[str, Any], body: InferredBody
) -> InferredBody:
    """Honour a printed component value for single-resistance bodies."""
    if len(body.lines) != 1 or not body.lines[0].startswith("R1 "):
        return body
    raw = str(component.get("value") or "").strip()
    match = _VALUE_RE.match(raw)
    if not match:
        return body
    value = match.group(1) + (match.group(2) or "").lower()
    return InferredBody(
        lines=(f"R1 p1 p2 {value}",),
        rationale=body.rationale + " (printed value)",
        exact_pins=body.exact_pins,
    )
