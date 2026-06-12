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
    """EEvision wire classification from the printed net identity."""
    for candidate in (net.get("name"), net.get("wireId")):
        text = str(candidate or "").strip()
        if not text:
            continue
        if _GROUND_NAME_RE.match(text) or _MIL_GROUND_WIRE_RE.match(text):
            return "ground"
        if _POWER_RE.search(text):
            return "power"
    return ""


def net_label(net: dict[str, Any], index: int) -> str:
    """Display name for a net: printed name, else wire id, else N###."""
    return str(net.get("name") or net.get("wireId") or f"N{index:03d}")


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


#: Column order of the generated excel2edb table.
_CSV_HEADERS = [
    "Wire",
    "Name",
    "Type",
    "Wire:Gauge",
    "Signal",
    "A-Comp",
    "A-CompName",
    "A-CompType",
    "A-Comp: imagedsp",
    "A-Comp:Part No",
    "A-Comp:Component Type",
    "A-Conn",
    "A-Cav",
    "A-CavName",
    "B-Comp",
    "B-CompName",
    "B-CompType",
    "B-Comp: imagedsp",
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
    converter's repeated-values rule.
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
    membership_counter: dict[str, int] = dict.fromkeys(components, 0)
    described: set[str] = set()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADERS)

    for index, net in enumerate(nets, start=1):
        wire_id = safe_id(net.get("wireId") or net.get("id"), f"W{index:03d}")
        label = net_label(net, index)
        endpoints: list[tuple[str, str]] = []  # (component id, cavity id)
        for node in net.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            comp_id = str(node.get("component") or "")
            if comp_id not in components:
                continue
            membership_counter[comp_id] += 1
            cavity = cavities[comp_id].get(membership_counter[comp_id], "c1")
            endpoints.append((comp_id, cavity))

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
                comp_id, cavity = endpoint
                row[f"{side}-Comp"] = safe_id(comp_id, comp_id)
                row[f"{side}-Conn"] = "A"
                row[f"{side}-Cav"] = cavity
                row[f"{side}-CavName"] = cavity
                if comp_id not in described:
                    described.add(comp_id)
                    row.update(_component_fields(side, components[comp_id]))
            writer.writerow([row.get(header, "") for header in _CSV_HEADERS])

    # No-wire entries for components that touch no net (parts-only rows).
    for comp_id, component in components.items():
        if membership_counter.get(comp_id):
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
    symbol = din_symbol(component)
    if symbol:
        fields[f"{side}-Comp: imagedsp"] = f"{symbol},40,40"
    if component.get("partNumber"):
        fields[f"{side}-Comp:Part No"] = str(component["partNumber"])
    if component.get("type"):
        fields[f"{side}-Comp:Component Type"] = str(component["type"])
    return fields
