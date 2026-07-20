"""Render a Captify schematic graph as a standalone XML document.

A plain, self-describing XML serialization of the ENTIRE ``captify.schematic.v1``
graph — title block, every component (with pins and drawing locations), and
every net (with memberships, attachment points, and wire segments) — for
consumers that want the whole schematic in one portable file without parsing
JSON or an EDA-specific format. Like :mod:`netlist` and :mod:`edml`, this is a
pure deterministic serializer with no schematic understanding of its own.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any


def graph_to_xml(graph: dict[str, Any], *, source_name: str) -> str:
    """Serialize a ``captify.schematic.v1`` graph to an XML string."""
    root = ET.Element(
        "schematic",
        {
            "format": "captify.schematic.v1",
            "source": source_name,
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    _set_optional(root, "confidence", graph.get("confidence"))

    title = graph.get("titleBlock")
    if isinstance(title, dict):
        title_el = ET.SubElement(root, "titleBlock")
        for key, value in title.items():
            if value is not None:
                ET.SubElement(title_el, key).text = str(value)

    pages = graph.get("pages")
    if isinstance(pages, list) and pages:
        pages_el = ET.SubElement(root, "pages")
        for page in pages:
            if isinstance(page, dict):
                page_el = ET.SubElement(pages_el, "page")
                _set_optional(page_el, "number", page.get("pageNumber"))

    components_el = ET.SubElement(root, "components")
    for component in graph.get("components") or []:
        if isinstance(component, dict):
            _component_element(components_el, component)

    nets_el = ET.SubElement(root, "nets")
    for net in graph.get("nets") or []:
        if isinstance(net, dict):
            _net_element(nets_el, net)

    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )


def _component_element(parent: ET.Element, component: dict[str, Any]) -> None:
    comp_el = ET.SubElement(parent, "component")
    for name in (
        "id",
        "refDes",
        "type",
        "value",
        "partNumber",
        "location",
        "parentComponent",
        "page",
        "confidence",
    ):
        _set_optional(comp_el, name, component.get(name))
    if component.get("description"):
        ET.SubElement(comp_el, "description").text = str(component["description"])
    bbox = component.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        ET.SubElement(
            comp_el,
            "bbox",
            {k: str(v) for k, v in zip(("x0", "y0", "x1", "y1"), bbox)},
        )
    pins = [p for p in component.get("pins") or [] if isinstance(p, dict)]
    if pins:
        pins_el = ET.SubElement(comp_el, "pins")
        for pin in pins:
            pin_el = ET.SubElement(pins_el, "pin")
            _set_optional(pin_el, "number", pin.get("number"))
            _set_optional(pin_el, "name", pin.get("name"))
            _set_optional(pin_el, "status", pin.get("status"))


def _net_element(parent: ET.Element, net: dict[str, Any]) -> None:
    net_el = ET.SubElement(parent, "net")
    for name in ("id", "name", "class", "wireId", "gauge", "signalType", "page"):
        _set_optional(net_el, name, net.get(name))
    for node in net.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_el = ET.SubElement(net_el, "node")
        _set_optional(node_el, "component", node.get("component"))
        _set_optional(node_el, "pin", node.get("pin"))
        _set_optional(node_el, "pinSource", node.get("pinSource"))
        attachment = node.get("attachment")
        if isinstance(attachment, (list, tuple)) and len(attachment) == 2:
            node_el.set("x", str(attachment[0]))
            node_el.set("y", str(attachment[1]))
    segments = net.get("segments")
    if isinstance(segments, list) and segments:
        segments_el = ET.SubElement(net_el, "segments")
        for segment in segments:
            if isinstance(segment, (list, tuple)) and len(segment) == 4:
                ET.SubElement(
                    segments_el,
                    "segment",
                    {k: str(v) for k, v in zip(("x1", "y1", "x2", "y2"), segment)},
                )


def _set_optional(element: ET.Element, name: str, value: Any) -> None:
    if value is not None and value != "":
        element.set(name, str(value))
