"""Deterministic connectivity identifiers: wire IDs and safe pin designators.

Downstream EDA importers (EE Vision's kbl2edb, IPC-2581 consumers) need
pin-level from-to connectivity and per-wire identifiers. A reverse-engineered
drawing rarely prints either, so this module assigns the parts that are
BOOKKEEPING, never physics claims:

* **Wire IDs** — every net gets a stable ``W###`` identifier when the drawing
  didn't print one (``wireIdSource: "assigned"`` marks it as ours).
* **Pin designators for 2-terminal parts** — a resistor/capacitor/diode/…
  has exactly two interchangeable-by-position terminals; numbering them
  1/2 by drawing position (leftmost/topmost first) is the universal
  convention and cannot mis-wire anything (``pinSource: "assigned"``).

Multi-pin devices (ICs, relays, multi-way switches) are NOT guessed — wrong
pin numbers on a microcontroller would be silently dangerous. Those stay
null until the engineer (or a vendor symbol/model) supplies them.
"""

from __future__ import annotations

from typing import Any

#: Component-type tokens whose parts have exactly two equivalent terminals.
TWO_TERMINAL_TOKENS = (
    "resistor",
    "capacitor",
    "inductor",
    "coil",
    "diode",
    "led",
    "fuse",
    "lamp",
    "crystal",
    "buzzer",
    "speaker",
    "battery",
    "thermistor",
)


def assign_wire_ids(graph: dict[str, Any]) -> int:
    """Give every net without a printed wire id a stable assigned one.

    ``W001…`` in net order (deterministic across re-exports of the same
    graph). Returns how many were assigned.
    """
    assigned = 0
    for index, net in enumerate(graph.get("nets") or [], start=1):
        if not isinstance(net, dict) or net.get("wireId"):
            continue
        net["wireId"] = f"W{index:03d}"
        net["wireIdSource"] = "assigned"
        assigned += 1
    return assigned


def assign_two_terminal_pins(graph: dict[str, Any]) -> int:
    """Assign 1/2 pin designators to 2-terminal components' net memberships.

    Applies only when the component (a) is a 2-terminal class, (b) appears in
    at most two net memberships, and (c) none of its memberships already
    carry a pin (drawing-printed or model-claimed pins always win). Pin 1 is
    the leftmost/topmost attachment — the positional convention. The
    component's ``pins`` list is seeded to match, so exporters number
    cavities consistently. Returns memberships that gained a pin.
    """
    components = {
        str(c.get("id")): c
        for c in graph.get("components") or []
        if isinstance(c, dict)
    }
    memberships: dict[str, list[dict[str, Any]]] = {}
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        for node in net.get("nodes") or []:
            if isinstance(node, dict) and node.get("component"):
                memberships.setdefault(str(node["component"]), []).append(node)

    assigned = 0
    for comp_id, nodes in memberships.items():
        component = components.get(comp_id)
        if component is None or not _is_two_terminal(component):
            continue
        if len(nodes) > 2 or any(node.get("pin") for node in nodes):
            continue
        ordered = sorted(nodes, key=_attachment_order)
        for pin_number, node in enumerate(ordered, start=1):
            node["pin"] = str(pin_number)
            node["pinSource"] = "assigned"
            assigned += 1
        if not component.get("pins"):
            component["pins"] = [{"number": "1"}, {"number": "2"}]
    return assigned


#: Cap the embedded QA worklist so a huge sheet can't bloat the graph.
_MAX_QA_WORKLIST = 200


def record_connectivity_quality(graph: dict[str, Any]) -> dict[str, Any]:
    """Stamp a machine-readable connectivity QA block onto the graph.

    ``connectivityQuality`` answers "how trustworthy is the from-to data and
    what still needs an engineer?" in one place: membership counts, pin
    provenance histogram, and a ``qaWorklist`` of every unpinned membership
    (component, net, attachment point) — the exact list an agent can walk
    with an engineer to finish pin assignment.
    """
    components = {
        str(c.get("id")): c
        for c in graph.get("components") or []
        if isinstance(c, dict)
    }
    by_source: dict[str, int] = {}
    worklist: list[dict[str, Any]] = []
    total = 0
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        for node in net.get("nodes") or []:
            if not isinstance(node, dict) or not node.get("component"):
                continue
            total += 1
            if node.get("pin"):
                source = str(node.get("pinSource") or "model")
                by_source[source] = by_source.get(source, 0) + 1
                continue
            component = components.get(str(node["component"])) or {}
            if len(worklist) < _MAX_QA_WORKLIST:
                worklist.append(
                    {
                        "component": node.get("component"),
                        "refDes": component.get("refDes"),
                        "componentType": component.get("type"),
                        "net": net.get("id"),
                        "wireId": net.get("wireId"),
                        "netName": net.get("name"),
                        "attachment": node.get("attachment"),
                        "page": net.get("page"),
                    }
                )
    pinned = sum(by_source.values())
    quality = {
        "membershipCount": total,
        "pinnedCount": pinned,
        "pinCoverage": round(pinned / total, 3) if total else None,
        "pinSourceCounts": by_source,
        "unpinnedCount": total - pinned,
        "qaWorklist": worklist,
    }
    graph["connectivityQuality"] = quality
    return quality


def _is_two_terminal(component: dict[str, Any]) -> bool:
    ctype = str(component.get("type") or "").lower()
    return any(token in ctype for token in TWO_TERMINAL_TOKENS)


def _attachment_order(node: dict[str, Any]) -> tuple[float, float]:
    attachment = node.get("attachment")
    if isinstance(attachment, (list, tuple)) and len(attachment) == 2:
        return float(attachment[0]), float(attachment[1])
    return (float("inf"), float("inf"))
