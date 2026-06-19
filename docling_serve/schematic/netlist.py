"""Render a Captify schematic graph as a KiCad-style netlist (.net).

KiCad's netlist S-expression is a widely importable EDA interchange format, so
emitting one lets the extracted connectivity be re-opened in CAD tools. The
graph itself is model-derived (see :mod:`schematic_extractor`); this module is a
pure, deterministic serializer with no schematic "understanding" of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _q(value: Any) -> str:
    """Quote a value for a KiCad S-expression atom."""
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def graph_to_kicad_netlist(graph: dict[str, Any], *, source_name: str) -> str:
    """Serialize a ``captify.schematic.v1`` graph to a KiCad netlist string."""
    components = graph.get("components") or []
    nets = graph.get("nets") or []

    # Map component id -> reference designator (fall back to the id).
    ref_by_id: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict):
            continue
        comp_id = str(component.get("id") or "")
        ref = component.get("refDes") or comp_id
        if comp_id:
            ref_by_id[comp_id] = str(ref)

    lines: list[str] = []
    lines.append('(export (version "E")')
    lines.append("  (design")
    lines.append(f"    (source {_q(source_name)})")
    lines.append(f"    (date {_q(datetime.now(UTC).isoformat())})")
    lines.append('    (tool "captify-docling-serve schematic extractor"))')

    lines.append("  (components")
    for component in components:
        if not isinstance(component, dict):
            continue
        ref = component.get("refDes") or component.get("id")
        if not ref:
            continue
        comp_lines = [f"    (comp (ref {_q(ref)})"]
        if component.get("value") is not None:
            comp_lines.append(f"      (value {_q(component.get('value'))})")
        if component.get("type") is not None:
            comp_lines.append(f"      (footprint {_q(component.get('type'))})")
        if component.get("description") is not None:
            comp_lines.append(f"      (description {_q(component.get('description'))})")
        comp_lines[-1] = comp_lines[-1] + ")"
        lines.extend(comp_lines)
    lines.append("  )")

    lines.append("  (nets")
    for index, net in enumerate(nets, start=1):
        if not isinstance(net, dict):
            continue
        name = net.get("name") or net.get("id") or f"NET{index}"
        lines.append(f"    (net (code {_q(index)}) (name {_q(name)})")
        for node in net.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            component = node.get("component")
            ref = ref_by_id.get(str(component), str(component) if component else "")
            if not ref:
                continue
            pin = node.get("pin")
            pin_value = pin if pin not in (None, "") else "1"
            lines.append(f"      (node (ref {_q(ref)}) (pin {_q(pin_value)}))")
        lines.append("    )")
    lines.append("  )")
    lines.append(")")
    return "\n".join(lines) + "\n"
