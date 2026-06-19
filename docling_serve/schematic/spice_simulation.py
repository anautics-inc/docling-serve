"""Generic schematic simulation: classify the drawing, build a deck, run ngspice.

Works for ANY extracted schematic, with honesty about what a simulation can
mean at each fidelity level:

* **electromechanical-power** (relays, solenoids, valves, fuses, switches —
  e.g. aircraft armament/power distribution): DC operating point is genuinely
  meaningful. Coils are R+L (conduct at DC), contacts/fuses are milliohm
  resistances — energize a bus wire and the solve shows which loads are live.
* **digital-logic** (microcontrollers, logic ICs, displays): the DC solve
  verifies power rails and passive networks; logic BEHAVIOR needs vendor IC
  models (attach them to Part Catalog Items and they bind automatically).
* **analog-mixed**: passives + discrete semiconductors solve with inferred or
  vendor models; fidelity follows model coverage.

Stimulus is never invented silently: supplies are auto-detected from net
NAMES (``+5V``, ``28VDC BUS``, ``VCC`` …) and grounds from net names plus the
MIL-W-5088 wire-code convention (wire ids ending in ``N`` are ground wires —
how aircraft drawings mark returns). Anything auto-detected is reported, and
callers can override or supply sources explicitly ("energize THIS wire with
28 V"). SPICE needs a reference node: the ground net ties to node 0.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: Component-type keywords voting for each schematic kind, with vote weight.
#: Diagnostic types (a relay, an IC) weigh more than ubiquitous passives —
#: every board has resistors; only logic boards have logic.
_KIND_VOTES: dict[str, tuple[int, tuple[str, ...]]] = {
    "electromechanical-power": (
        3,
        (
            "relay",
            "solenoid",
            "valve",
            "contactor",
            "fuse",
            "breaker",
            "motor",
            "booster",
            "transformer",
            "lamp",
            "actuator",
        ),
    ),
    "digital-logic": (
        3,
        ("ic", "microcontroller", "display", "logic", "processor", "fpga"),
    ),
    "analog-mixed": (
        1,
        ("transistor", "diode", "capacitor", "inductor", "amplifier", "led"),
    ),
}

#: Net-name patterns that mark a DC supply, with how to read its voltage.
_SUPPLY_RE = re.compile(r"(?:^|[^A-Z0-9])(\+?(\d+(?:\.\d+)?)\s*V(?:DC)?)(?:[^A-Z0-9]|$)", re.IGNORECASE)
_SUPPLY_NAME_RE = re.compile(r"^(VCC|VDD|VBUS|VBAT|V\+|\+V|PWR|POWER)$", re.IGNORECASE)
_GROUND_NAME_RE = re.compile(r"^(GND|GROUND|VSS|CHASSIS|EARTH|COM|COMMON|0V|RETURN)$", re.IGNORECASE)
#: MIL-W-5088 aircraft wire codes: the trailing N segment marks a ground wire.
_MIL_GROUND_WIRE_RE = re.compile(r"^[A-Z0-9]+N$", re.IGNORECASE)

_DEFAULT_VOLTS_BY_KIND = {
    "electromechanical-power": 28.0,  # MIL-STD-704 28 V DC bus
    "digital-logic": 5.0,
    "analog-mixed": 5.0,
    "unknown": 5.0,
}


@dataclass
class SchematicClassification:
    """What kind of circuit this is and what simulation can mean for it."""

    kind: str
    rationale: str
    fidelity: str
    typeHistogram: dict[str, int] = field(default_factory=dict)


@dataclass
class SimulationResult:
    classification: SchematicClassification
    supplies: list[dict[str, Any]] = field(default_factory=list)
    grounds: list[str] = field(default_factory=list)
    nodeVoltages: dict[str, float] = field(default_factory=dict)
    sourceCurrents: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    engine: str = "ngspice"
    ok: bool = False
    log: str = ""


def classify_schematic(graph: dict[str, Any]) -> SchematicClassification:
    """Heuristic circuit-kind classification from the component-type mix."""
    histogram: Counter[str] = Counter()
    votes: Counter[str] = Counter()
    for component in graph.get("components") or []:
        if not isinstance(component, dict):
            continue
        ctype = str(component.get("type") or "other").strip().lower()
        histogram[ctype] += 1
        for kind, (weight, keywords) in _KIND_VOTES.items():
            if any(keyword in ctype for keyword in keywords):
                votes[kind] += weight
                break

    if not votes:
        kind = "unknown"
        rationale = "no recognizable component types"
    else:
        kind, count = votes.most_common(1)[0]
        leaders = ", ".join(
            f"{c} {t}" for t, c in histogram.most_common(4)
        )
        rationale = f"{count} components vote {kind} (dominant types: {leaders})"

    fidelity = {
        "electromechanical-power": (
            "DC operating point is meaningful: coils/contacts/fuses conduct, so "
            "energizing a bus shows which loads are live."
        ),
        "digital-logic": (
            "DC solve verifies power rails and passive networks; logic behavior "
            "requires vendor IC models on the part catalog."
        ),
        "analog-mixed": (
            "Discrete semiconductors solve with inferred or vendor models; "
            "fidelity follows model coverage."
        ),
        "unknown": "Connectivity-level solve only.",
    }[kind]
    return SchematicClassification(
        kind=kind,
        rationale=rationale,
        fidelity=fidelity,
        typeHistogram=dict(histogram.most_common(12)),
    )


def detect_power_nets(
    graph: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Auto-detected (supplies, ground net ids) from net names.

    Supplies: nets whose printed name carries a voltage (``28VDC BUS``,
    ``+5V``) or a conventional rail name (``VCC`` → kind-default volts).
    Grounds: conventional names plus MIL-W-5088 ``…N`` wire ids.
    """
    supplies: list[dict[str, Any]] = []
    grounds: list[str] = []
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        net_id = str(net.get("id") or "")
        for candidate in (net.get("name"), net.get("wireId")):
            text = str(candidate or "").strip()
            if not text:
                continue
            if _GROUND_NAME_RE.match(text) or _MIL_GROUND_WIRE_RE.match(text):
                grounds.append(net_id)
                break
            voltage_hit = _SUPPLY_RE.search(text)
            if voltage_hit:
                supplies.append(
                    {"net": net_id, "name": text, "volts": float(voltage_hit.group(2))}
                )
                break
            if _SUPPLY_NAME_RE.match(text):
                supplies.append({"net": net_id, "name": text, "volts": None})
                break
    return supplies, grounds


def simulate_graph(
    graph: dict[str, Any],
    *,
    source_name: str,
    sources: list[dict[str, Any]] | None = None,
    timeout_s: float = 120.0,
) -> SimulationResult:
    """DC operating-point simulation of an extracted schematic graph.

    ``sources`` overrides/extends auto-detection: ``[{"net": <id or name>,
    "volts": 28.0}]`` — "energize this wire". Returns node voltages from a
    real ngspice solve, never fabricated numbers.
    """
    from docling_serve.schematic.spice import graph_to_spice

    classification = classify_schematic(graph)
    result = SimulationResult(classification=classification)
    if not shutil.which("ngspice"):
        result.warnings.append("ngspice is not installed on the extraction host")
        return result

    auto_supplies, grounds = detect_power_nets(graph)
    default_volts = _DEFAULT_VOLTS_BY_KIND[classification.kind]
    for supply in auto_supplies:
        if supply["volts"] is None:
            supply["volts"] = default_volts
            supply["assumed"] = True

    by_name = _net_lookup(graph)
    explicit: list[dict[str, Any]] = []
    for requested in sources or []:
        if not isinstance(requested, dict):
            continue
        net_id = by_name.get(str(requested.get("net") or "").strip().upper())
        if net_id is None:
            result.warnings.append(f"source net {requested.get('net')!r} not found")
            continue
        volts = float(requested.get("volts") or default_volts)
        explicit.append({"net": net_id, "name": requested.get("net"), "volts": volts})
    supplies = explicit or auto_supplies
    if not supplies:
        result.warnings.append(
            "no supply net detected or specified — select a wire and a voltage "
            'to energize (e.g. {"net": "A12C18", "volts": 28})'
        )
        return result

    if not grounds:
        ground_id = _most_connected_net(graph, exclude={s["net"] for s in supplies})
        if ground_id is None:
            result.warnings.append("no ground reference available")
            return result
        grounds = [ground_id]
        result.warnings.append(
            f"no ground net detected; using the most-connected net {ground_id} as reference"
        )

    result.supplies = supplies
    result.grounds = grounds

    netlist = graph_to_spice(graph, source_name=source_name)
    deck = _build_deck(netlist, graph, supplies=supplies, grounds=grounds)
    ok, voltages, currents, log = _run_ngspice(deck, timeout_s=timeout_s)
    result.ok = ok
    result.nodeVoltages = voltages
    result.sourceCurrents = currents
    result.log = log if not ok else ""
    if not ok:
        result.warnings.append("ngspice did not converge or rejected the deck")
    return result


def _net_lookup(graph: dict[str, Any]) -> dict[str, str]:
    """UPPER(name|wireId|id) -> net id."""
    lookup: dict[str, str] = {}
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        net_id = str(net.get("id") or "")
        for key in (net.get("id"), net.get("name"), net.get("wireId")):
            text = str(key or "").strip().upper()
            if text:
                lookup.setdefault(text, net_id)
    return lookup


def _most_connected_net(
    graph: dict[str, Any], *, exclude: set[str]
) -> str | None:
    best: tuple[int, str] | None = None
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        net_id = str(net.get("id") or "")
        if net_id in exclude:
            continue
        size = len(net.get("nodes") or [])
        if best is None or size > best[0]:
            best = (size, net_id)
    return best[1] if best else None


def _spice_node(graph: dict[str, Any], net_id: str) -> str:
    """The SPICE node name the exporter assigns to a net (same sanitizer)."""

    # The exporter derives node names per net in order; rebuild the mapping.
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]
    for index, net in enumerate(nets, start=1):
        if str(net.get("id")) == net_id:
            raw = net.get("name") or net.get("wireId") or f"N{index:03d}"
            return re.sub(r"[^A-Za-z0-9_]", "_", str(raw).strip()).strip("_") or "X"
    return net_id


def _build_deck(
    netlist: str,
    graph: dict[str, Any],
    *,
    supplies: list[dict[str, Any]],
    grounds: list[str],
) -> str:
    """The runnable .op deck: netlist + stimulus + ground reference + control."""
    stimulus: list[str] = ["", "* simulation stimulus (auto-detected / user-specified)"]
    # Reverse-engineered netlists always carry floating nodes (unconnected
    # pins, off-page stubs). A global node-to-ground shunt keeps the matrix
    # non-singular without measurably loading any real path.
    stimulus.append(".option rshunt=1e9")
    # SPICE requires node 0: tie each detected ground net to it. ngspice
    # already aliases the node literally named "gnd" to 0 — tying that one
    # would be a shorted source, so it's skipped.
    for index, ground in enumerate(grounds):
        node = _spice_node(graph, ground)
        if node.lower() in ("0", "gnd"):
            continue
        label = "V_GNDREF" if index == 0 else f"V_GND_{node}"
        stimulus.append(f"{label} {node} 0 DC 0")
    for index, supply in enumerate(supplies, start=1):
        node = _spice_node(graph, supply["net"])
        stimulus.append(f"V_SUP{index} {node} 0 DC {supply['volts']}")
    control = "\n".join(
        ["", ".control", "op", "print all", "quit", ".endc", ""]
    )
    return netlist.replace("\n.end\n", "\n" + "\n".join(stimulus) + control + ".end\n")


#: Names ``print all`` emits that are simulator constants, not circuit nodes.
_NGSPICE_CONSTANTS = frozenset(
    {"false", "true", "boltz", "c", "e", "echarge", "kelvin", "no", "pi", "planck", "yes", "i"}
)


def _run_ngspice(
    deck: str, *, timeout_s: float
) -> tuple[bool, dict[str, float], dict[str, float], str]:
    with tempfile.NamedTemporaryFile("w", suffix=".cir", delete=False) as handle:
        handle.write(deck)
        path = handle.name
    try:
        completed = subprocess.run(
            ["ngspice", "-b", path], capture_output=True, text=True, timeout=timeout_s
        )
    finally:
        Path(path).unlink(missing_ok=True)
    output = completed.stdout + completed.stderr
    lowered = output.lower()
    failed = completed.returncode != 0 or "error" in lowered.replace("no error", "")
    voltages: dict[str, float] = {}
    currents: dict[str, float] = {}
    for line in output.splitlines():
        match = re.match(r"^\s*([a-z0-9_.#@\[\]]+)\s*=\s*([-+0-9.eE]+)\s*$", line.strip(), re.IGNORECASE)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if name.lower() in _NGSPICE_CONSTANTS:
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if name.lower().startswith(("v_sup", "v_gnd")) or "#branch" in name.lower():
            currents[name] = number
        else:
            voltages[name] = number
    return (not failed) and bool(voltages), voltages, currents, output[-2000:]
