"""EEvision delivery helpers + the excel2edb CSV emitter.

Altair EEvision ingests two text formats we can generate completely (per the
vendor's "Creating EEvision Files" documentation):

* **EDML** — their electrical design modeling language, compiled by
  ``edml2edb`` (see :mod:`edml`, which uses the helpers here), and
* the **Excel-to-EDB table** (``excel2edb``): one row per wire with its two
  extremities in ``A-*`` / ``B-*`` column groups; nets touching more than two
  points repeat the wire id across rows (the documented multi-term form).

Shared mapping decisions live here so both emitters agree:

* every drawing component becomes an EEvision ``ECU``-class device with one
  connector whose cavities are the component's PIN DESIGNATORS (the pins the
  pipeline recovers from print/vision/convention),
* component types map onto EEvision's built-in DIN 40719-2 symbol letters
  (``R`` resistor, ``K`` relay, ``V`` semiconductor, …) via ``Imagedsp``,
* wires classify as ``power`` / ``ground`` / ``logical`` from their printed
  names (MIL-W-5088 ``…N`` wire ids are grounds).
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

#: Component-type keyword -> EEvision built-in DIN 40719-2 symbol letter.
DIN_SYMBOL_MAP: tuple[tuple[str, str], ...] = (
    ("resistor", "R"),
    ("capacitor", "C"),
    ("inductor", "L"),
    ("coil", "L"),
    ("solenoid", "L"),
    ("valve", "L"),
    ("transformer", "L"),
    ("relay", "K"),
    ("contactor", "K"),
    ("switch", "S"),
    ("button", "S"),
    ("breaker", "S"),
    ("diode", "V"),
    ("led", "V"),
    ("transistor", "V"),
    ("rectifier", "V"),
    ("fuse", "F"),
    ("motor", "M"),
    ("pump", "M"),
    ("fan", "M"),
    ("booster", "M"),
    ("lamp", "H"),
    ("light", "H"),
    ("indicator", "H"),
    ("annunciator", "H"),
    ("buzzer", "H"),
    ("speaker", "H"),
    ("battery", "G"),
    ("generator", "G"),
    ("supply", "G"),
    ("meter", "P"),
    ("measurement", "P"),
    ("ic", "A"),
    ("microcontroller", "A"),
    ("display", "A"),
)

_GROUND_NAME_RE = re.compile(r"^(GND|GROUND|VSS|CHASSIS|EARTH|COM|COMMON|0V|RETURN)$", re.IGNORECASE)
_MIL_GROUND_WIRE_RE = re.compile(r"^[A-Z0-9]+N$", re.IGNORECASE)
_POWER_RE = re.compile(r"(\d+(?:\.\d+)?\s*V(?:DC)?\b)|^(VCC|VDD|VBUS|VBAT|PWR|POWER|\+\d)", re.IGNORECASE)
_HV_RE = re.compile(r"^(HV|HIGH[ _-]?VOLTAGE)([_\s-]|$)|[_\s-]HV$", re.IGNORECASE)
# NOTE: "_" is a regex word character, so \b never fires around it — bus names
# like CAN_BUS need explicit separator classes instead of word boundaries.
_BUS_RE = re.compile(
    r"^(CAN|LIN|RS[- ]?485|RS[- ]?232|MIL[- ]?STD[- ]?1553)([_\s-]|$)|(^|[_\s-])BUS([_\s-]|$)",
    re.IGNORECASE,
)

#: Graph-level ``class`` / ``signalType`` values -> EEvision wire types
#: (``EdbWireType`` letters minus ARC, which is derived from topology).
_NET_CLASS_MAP: dict[str, str] = {
    "power": "power",
    "ground": "ground",
    "signal": "logical",
    "logical": "logical",
    "bus": "bus",
    "hv": "hv",
}

_ID_RE = re.compile(r"[^0-9A-Za-z_]+")


def safe_id(value: Any, fallback: str) -> str:
    """An EDML/EDB-safe identifier: digits, letters, underscores only."""
    cleaned = _ID_RE.sub("_", str(value or "").strip()).strip("_")
    return cleaned or fallback


def din_symbol(component: dict[str, Any]) -> str | None:
    """The built-in EEvision symbol letter for a component type, if any."""
    ctype = str(component.get("type") or "").lower()
    for token, letter in DIN_SYMBOL_MAP:
        if token in ctype:
            return letter
    return None


def wire_type(net: dict[str, Any]) -> str:
    """EEvision wire classification for one net.

    The extractor's own judgment wins: the graph's ``class`` / ``signalType``
    fields carry the vision+convention classification (``HV`` nets are classed
    ``power`` even though no name regex can tell). Printed-name heuristics are
    the fallback for graphs that predate net classification.
    """
    for candidate in (net.get("class"), net.get("signalType")):
        mapped = _NET_CLASS_MAP.get(str(candidate or "").strip().lower())
        if mapped:
            return mapped
    for candidate in (net.get("name"), net.get("wireId")):
        text = str(candidate or "").strip()
        if not text:
            continue
        if _GROUND_NAME_RE.match(text) or _MIL_GROUND_WIRE_RE.match(text):
            return "ground"
        if _HV_RE.search(text):
            return "hv"
        if _BUS_RE.search(text):
            return "bus"
        if _POWER_RE.search(text):
            return "power"
    return ""


def net_label(net: dict[str, Any], index: int) -> str:
    """Display name for a net: printed name, else wire id, else N###."""
    return str(net.get("name") or net.get("wireId") or f"N{index:03d}")


def net_connection_plan(
    nets: list[dict[str, Any]],
    component_ids: set[str],
    cavities: dict[str, dict[int, str]],
) -> list[dict[str, Any]]:
    """Split each net's memberships into wire endpoints and internal arcs.

    When one component touches the same net more than once, its FIRST
    membership stays a wire endpoint; every additional membership becomes a
    component-internal connection — EEvision's ARC concept (a fuse's two
    cavities, a switch pole) — between the first cavity and the extra one.
    Emitting these as arcs instead of repeated joins keeps the schematic
    clean and lets EEvision's Net Mode trace through the component.

    Returns one entry per net: ``{"endpoints": [(comp, cavity)], "arcs":
    [(comp, first_cavity, extra_cavity)]}``. Membership numbering matches
    :func:`cavity_ids_for`, so cavity ids are stable across emitters.
    """
    counters: dict[str, int] = dict.fromkeys(component_ids, 0)
    plan: list[dict[str, Any]] = []
    for net in nets:
        endpoints: list[tuple[str, str]] = []
        arcs: list[tuple[str, str, str]] = []
        first_cavity: dict[str, str] = {}
        for node in net.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            comp_id = str(node.get("component") or "")
            if comp_id not in component_ids:
                continue
            counters[comp_id] += 1
            cavity = cavities[comp_id].get(counters[comp_id], "c1")
            if comp_id in first_cavity:
                arcs.append((comp_id, first_cavity[comp_id], cavity))
            else:
                first_cavity[comp_id] = cavity
                endpoints.append((comp_id, cavity))
        plan.append({"endpoints": endpoints, "arcs": arcs})
    return plan


def cavity_ids_for(
    component_id: str, nets: list[dict[str, Any]]
) -> dict[int, str]:
    """Stable cavity id per (net-order) membership of one component.

    The cavity IS the pin when the membership carries a designator;
    memberships without pins get sequential ``c#`` cavities so they remain
    distinct connection points.
    """
    out: dict[int, str] = {}
    used: set[str] = set()
    counter = 0
    membership = 0
    for net in nets:
        for node in net.get("nodes") or []:
            if not isinstance(node, dict) or str(node.get("component")) != component_id:
                continue
            membership += 1
            pin = safe_id(node.get("pin"), "")
            if not pin or pin in used:
                counter += 1
                pin = f"c{counter}"
                while pin in used:
                    counter += 1
                    pin = f"c{counter}"
            used.add(pin)
            out[membership] = pin
    return out


#: Column order of the generated excel2edb table. Note there is NO
#: ``: imagedsp`` column: the Excel-to-EDB spec documents only ``" color"``
#: as an interpreted reserved attribute, so DIN symbols ride the EDML/EDB
#: paths (``Imagedsp`` property / ``" imagedsp"`` attribute) instead.
_CSV_HEADERS = [
    "Wire",
    "Name",
    "Type",
    "Wire:Gauge",
    "Signal",
    "A-Comp",
    "A-CompName",
    "A-CompType",
    "A-Comp:Part No",
    "A-Comp:Component Type",
    "A-Conn",
    "A-Cav",
    "A-CavName",
    "B-Comp",
    "B-CompName",
    "B-CompType",
    "B-Comp:Part No",
    "B-Comp:Component Type",
    "B-Conn",
    "B-Cav",
    "B-CavName",
]


def graph_to_eevision_csv(graph: dict[str, Any], *, source_name: str) -> str:
    """Serialize a ``captify.schematic.v1`` graph to an excel2edb CSV string.

    One row per wire extremity-pair; nets with more than two memberships
    repeat the wire id across rows (excel2edb's multi-term form). Repeated
    component/connector data is emitted only on first occurrence, per the
    converter's repeated-values rule. Duplicate memberships of one component
    on one net become ``ARC`` rows — the converter's model for electrical
    connections INSIDE a component — rather than a wire looping back.
    """
    del source_name  # the table format has no document-level fields
    components = {
        str(c.get("id")): c
        for c in graph.get("components") or []
        if isinstance(c, dict)
    }
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]

    # Pre-compute per-component cavity assignments (pin-first).
    cavities = {
        comp_id: cavity_ids_for(comp_id, nets) for comp_id in components
    }
    plan = net_connection_plan(nets, set(components), cavities)
    connected: set[str] = set()
    described: set[str] = set()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADERS)

    def endpoint_columns(row: dict[str, str], side: str, comp_id: str, cavity: str) -> None:
        row[f"{side}-Comp"] = safe_id(comp_id, comp_id)
        row[f"{side}-Conn"] = "A"
        row[f"{side}-Cav"] = cavity
        row[f"{side}-CavName"] = cavity
        if comp_id not in described:
            described.add(comp_id)
            row.update(_component_fields(side, components[comp_id]))

    arc_counter = 0
    for index, (net, entry) in enumerate(zip(nets, plan), start=1):
        wire_id = safe_id(net.get("wireId") or net.get("id"), f"W{index:03d}")
        label = net_label(net, index)
        endpoints: list[tuple[str, str]] = entry["endpoints"]
        connected.update(comp_id for comp_id, _ in endpoints)

        pairs = [endpoints[i : i + 2] for i in range(0, max(len(endpoints), 1), 2)]
        for row_no, pair in enumerate(pairs):
            row = {
                "Wire": wire_id,
                "Name": label if row_no == 0 else "",
                "Type": (wire_type(net).upper() if row_no == 0 else ""),
                "Wire:Gauge": (str(net.get("gauge") or "") if row_no == 0 else ""),
                "Signal": (str(net.get("name") or "") if row_no == 0 else ""),
            }
            for side, endpoint in zip(("A", "B"), pair):
                endpoint_columns(row, side, *endpoint)
            writer.writerow([row.get(header, "") for header in _CSV_HEADERS])

        # Component-internal connections (Type=ARC): both extremities on the
        # same component, connecting the extra membership back to the first.
        for comp_id, first_cavity, extra_cavity in entry["arcs"]:
            arc_counter += 1
            row = {"Wire": f"ARC{arc_counter:03d}", "Type": "ARC"}
            endpoint_columns(row, "A", comp_id, first_cavity)
            endpoint_columns(row, "B", comp_id, extra_cavity)
            writer.writerow([row.get(header, "") for header in _CSV_HEADERS])

    # No-wire entries for components that touch no net (parts-only rows).
    # Ground GLYPHS are net semantics, not parts — a "GND1" component row in
    # EE Vision's browser is noise, so they are excluded.
    for comp_id, component in components.items():
        if comp_id in connected:
            continue
        if "ground" in str(component.get("type") or "").lower():
            continue
        row = {"A-Comp": safe_id(comp_id, comp_id)}
        row.update(_component_fields("A", component))
        writer.writerow([row.get(header, "") for header in _CSV_HEADERS])

    return buffer.getvalue()


def _component_fields(side: str, component: dict[str, Any]) -> dict[str, str]:
    """First-occurrence component columns for one extremity side."""
    fields = {
        f"{side}-CompName": str(component.get("refDes") or component.get("id") or ""),
        f"{side}-CompType": "ECU",
    }
    if component.get("partNumber"):
        fields[f"{side}-Comp:Part No"] = str(component["partNumber"])
    if component.get("type"):
        fields[f"{side}-Comp:Component Type"] = str(component["type"])
    return fields
