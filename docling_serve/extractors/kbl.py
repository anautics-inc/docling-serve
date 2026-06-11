"""Render a Captify schematic graph as KBL (VDA 4964, "Kabelbaum Liste").

KBL is THE vendor-neutral harness exchange standard — and the documented
intermediate format Altair EEvision converts into its native EDB model, so a
schema-valid KBL file is the practical "open in EE Vision" artifact (EDML is
an internal text sketch, not an interchange standard).

The mapping from ``captify.schematic.v1``:

* component → ``Connector_housing`` part + ``Connector_occurrence`` whose
  slot cavities are the component's net attachment points,
* net → ``General_wire`` part + ``General_wire_occurrence`` + ``Connection``
  with one ``Extremity`` per member contact point (KBL requires ≥2 — nets
  with fewer resolvable ends are exported as wires without a connection),
* drawing title block → ``Harness`` title block.

Like :mod:`netlist` and :mod:`edml`, this is a pure deterministic serializer
validated against the official ``KBL24_SR1.xsd`` in the test suite.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

KBL_NAMESPACE = "http://www.prostep.org/Car_electric_container/KBL2.3/KBLSchema"

_UNIT_ID = "unit_metre"
_UNKNOWN = "UNKNOWN"


def graph_to_kbl(graph: dict[str, Any], *, source_name: str) -> str:
    """Serialize a ``captify.schematic.v1`` graph to a KBL 2.4 SR-1 string."""
    ET.register_namespace("kbl", KBL_NAMESPACE)
    root = ET.Element(f"{{{KBL_NAMESPACE}}}KBL_container")
    root.set("id", "kbl_root")
    root.set("version_id", "2.4")

    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]
    cavity_counts = _cavity_counts(components, nets)

    housing_ids, cavity_part_ids = _library_housings(root, components, cavity_counts)
    wire_part_id_by_net = _library_wires(root, nets)

    harness = _harness_header(root, graph, source_name)
    connector_elements, contact_point_ids = _connector_occurrences(
        components, housing_ids, cavity_part_ids
    )
    wire_elements, connection_elements = _wires_and_connections(
        nets, wire_part_id_by_net, contact_point_ids
    )
    # Harness children must follow the schema sequence order:
    # Connection before Connector_occurrence before General_wire_occurrence.
    for element in connection_elements + connector_elements + wire_elements:
        harness.append(element)

    # Units come after Harness in the container sequence.
    unit = ET.SubElement(root, "Unit")
    unit.set("id", _UNIT_ID)
    _text(unit, "Unit_name", "metre")
    _text(unit, "Si_unit_name", "metre")

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    ) + "\n"


def _cavity_counts(
    components: list[dict[str, Any]], nets: list[dict[str, Any]]
) -> dict[str, int]:
    """Cavities per component: max(known pins, net memberships), at least 1."""
    counts: dict[str, int] = {}
    for component in components:
        comp_id = str(component.get("id") or "")
        pins = [p for p in component.get("pins") or [] if isinstance(p, dict)]
        counts[comp_id] = max(len(pins), 1)
    for net in nets:
        usage: dict[str, int] = {}
        for node in net.get("nodes") or []:
            if isinstance(node, dict) and node.get("component"):
                comp_id = str(node["component"])
                usage[comp_id] = usage.get(comp_id, 0) + 1
        for comp_id, count in usage.items():
            counts[comp_id] = max(counts.get(comp_id, 1), count)
    return counts


def _library_housings(
    root: ET.Element,
    components: list[dict[str, Any]],
    cavity_counts: dict[str, int],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """``Connector_housing`` library parts with slot/cavity definitions."""
    housing_ids: dict[str, str] = {}
    cavity_part_ids: dict[str, list[str]] = {}
    for index, component in enumerate(components, start=1):
        comp_id = str(component.get("id") or f"C{index:04d}")
        housing_id = f"ch_{index}"
        housing_ids[comp_id] = housing_id
        housing = ET.SubElement(root, "Connector_housing")
        housing.set("id", housing_id)
        _part_fields(
            housing,
            part_number=component.get("partNumber") or comp_id,
            abbreviation=component.get("refDes") or comp_id,
            description=component.get("value")
            or component.get("description")
            or component.get("type")
            or comp_id,
        )
        slot = ET.SubElement(housing, "Slots")
        slot.set("id", f"{housing_id}_slot")
        count = cavity_counts.get(comp_id, 1)
        _text(slot, "Number_of_cavities", str(count))
        part_cavities: list[str] = []
        pins = [p for p in component.get("pins") or [] if isinstance(p, dict)]
        for cavity_index in range(count):
            cavity = ET.SubElement(slot, "Cavities")
            cavity_id = f"{housing_id}_cav{cavity_index + 1}"
            cavity.set("id", cavity_id)
            pin = pins[cavity_index] if cavity_index < len(pins) else None
            number = (pin or {}).get("number") or (pin or {}).get("name")
            _text(cavity, "Cavity_number", str(number or cavity_index + 1))
            part_cavities.append(cavity_id)
        cavity_part_ids[comp_id] = part_cavities
    return housing_ids, cavity_part_ids


def _library_wires(
    root: ET.Element, nets: list[dict[str, Any]]
) -> dict[str, str]:
    """``General_wire`` library parts, one per net."""
    wire_part_id_by_net: dict[str, str] = {}
    for index, net in enumerate(nets, start=1):
        net_id = str(net.get("id") or f"N{index:04d}")
        wire_id = f"w_{index}"
        wire_part_id_by_net[net_id] = wire_id
        wire = ET.SubElement(root, "General_wire")
        wire.set("id", wire_id)
        _part_fields(
            wire,
            part_number=net.get("wireId") or net.get("name") or net_id,
            abbreviation=net.get("name") or net_id,
            description=f"Wire {net.get('name') or net_id}",
        )
        if net.get("gauge"):
            _text(wire, "Wire_type", str(net["gauge"]))
        colour = ET.SubElement(wire, "Cover_colour")
        colour.set("id", f"{wire_id}_col")
        _text(colour, "Colour_type", _UNKNOWN)
        _text(colour, "Colour_value", _UNKNOWN)
    return wire_part_id_by_net


def _harness_header(
    root: ET.Element, graph: dict[str, Any], source_name: str
) -> ET.Element:
    harness = ET.SubElement(root, "Harness")
    harness.set("id", "harness_1")
    title = graph.get("titleBlock") if isinstance(graph.get("titleBlock"), dict) else {}
    _part_fields(
        harness,
        part_number=title.get("drawingNumber") or _slug(source_name),
        abbreviation=_slug(source_name)[:24],
        description=title.get("title") or source_name,
        version=title.get("revision") or "1",
    )
    _text(harness, "Car_classification_level_2", _UNKNOWN)
    _text(harness, "Model_year", _UNKNOWN)
    _text(harness, "Content", "harness complete set")
    return harness


def _connector_occurrences(
    components: list[dict[str, Any]],
    housing_ids: dict[str, str],
    cavity_part_ids: dict[str, list[str]],
) -> tuple[list[ET.Element], dict[str, list[str]]]:
    """Detached ``Connector_occurrence`` elements + contact point ids."""
    connector_elements: list[ET.Element] = []
    contact_point_ids: dict[str, list[str]] = {}
    for index, component in enumerate(components, start=1):
        comp_id = str(component.get("id") or f"C{index:04d}")
        occurrence = ET.Element("Connector_occurrence")
        connector_elements.append(occurrence)
        occurrence_id = f"occ_{index}"
        occurrence.set("id", occurrence_id)
        _text(occurrence, "Id", component.get("refDes") or comp_id)
        if component.get("location"):
            _text(occurrence, "Description", str(component["location"]))
        _text(occurrence, "Part", housing_ids[comp_id])

        cavity_occs: list[str] = []
        contact_points: list[str] = []
        for cavity_index, _cavity_part in enumerate(cavity_part_ids[comp_id], start=1):
            contact = ET.SubElement(occurrence, "Contact_points")
            contact_id = f"{occurrence_id}_cp{cavity_index}"
            contact.set("id", contact_id)
            _text(contact, "Id", str(cavity_index))
            cavity_occ_id = f"{occurrence_id}_cavocc{cavity_index}"
            _text(contact, "Contacted_cavity", cavity_occ_id)
            contact_points.append(contact_id)
            cavity_occs.append(cavity_occ_id)

        slots = ET.SubElement(occurrence, "Slots")
        slots.set("id", f"{occurrence_id}_slotocc")
        _text(slots, "Part", f"{housing_ids[comp_id]}_slot")
        for cavity_occ_id, cavity_part in zip(cavity_occs, cavity_part_ids[comp_id]):
            cavity_occ = ET.SubElement(slots, "Cavities")
            cavity_occ.set("id", cavity_occ_id)
            _text(cavity_occ, "Part", cavity_part)

        contact_point_ids[comp_id] = contact_points
    return connector_elements, contact_point_ids


def _wires_and_connections(
    nets: list[dict[str, Any]],
    wire_part_id_by_net: dict[str, str],
    contact_point_ids: dict[str, list[str]],
) -> tuple[list[ET.Element], list[ET.Element]]:
    """Detached ``General_wire_occurrence`` + ``Connection`` elements."""
    wire_elements: list[ET.Element] = []
    connection_elements: list[ET.Element] = []
    used_contacts: dict[str, int] = {}
    for index, net in enumerate(nets, start=1):
        net_id = str(net.get("id") or f"N{index:04d}")
        wire_occurrence = ET.Element("General_wire_occurrence")
        wire_elements.append(wire_occurrence)
        # The base type is abstract; plain wires are the Wire_occurrence
        # subtype, selected via xsi:type.
        wire_occurrence.set(
            "{http://www.w3.org/2001/XMLSchema-instance}type", "kbl:Wire_occurrence"
        )
        wire_occ_id = f"wocc_{index}"
        wire_occurrence.set("id", wire_occ_id)
        _text(wire_occurrence, "Part", wire_part_id_by_net[net_id])
        length = ET.SubElement(wire_occurrence, "Length_information")
        length.set("id", f"{wire_occ_id}_len")
        _text(length, "Length_type", "DMU")
        value = ET.SubElement(length, "Length_value")
        value.set("id", f"{wire_occ_id}_lenval")
        _text(value, "Unit_component", _UNIT_ID)
        _text(value, "Value_component", "0.0")
        _text(wire_occurrence, "Wire_number", net.get("name") or net_id)

        extremity_contacts = _claim_contacts(net, contact_point_ids, used_contacts)
        if len(extremity_contacts) < 2:
            continue  # KBL connections need both ends; wire part still exported

        connection = ET.Element("Connection")
        connection_elements.append(connection)
        connection.set("id", f"conn_{index}")
        _text(connection, "Id", net.get("name") or net_id)
        if net.get("name"):
            _text(connection, "Signal_name", str(net["name"]))
        if net.get("signalType"):
            _text(connection, "Signal_type", str(net["signalType"]))
        _text(connection, "Wire", wire_occ_id)
        for extremity_index, contact_id in enumerate(extremity_contacts, start=1):
            extremity = ET.SubElement(connection, "Extremities")
            extremity.set("id", f"conn_{index}_ext{extremity_index}")
            _text(extremity, "Position_on_wire", "0.0" if extremity_index == 1 else "1.0")
            _text(extremity, "Contact_point", contact_id)
    return wire_elements, connection_elements


def _claim_contacts(
    net: dict[str, Any],
    contact_point_ids: dict[str, list[str]],
    used_contacts: dict[str, int],
) -> list[str]:
    """One contact point per net membership, claimed in cavity order."""
    claimed: list[str] = []
    for node in net.get("nodes") or []:
        if not isinstance(node, dict) or not node.get("component"):
            continue
        comp_id = str(node["component"])
        available = contact_point_ids.get(comp_id) or []
        if not available:
            continue
        slot_index = min(used_contacts.get(comp_id, 0), len(available) - 1)
        used_contacts[comp_id] = slot_index + 1
        claimed.append(available[slot_index])
    return claimed


def _part_fields(
    element: ET.Element,
    *,
    part_number: str,
    abbreviation: str,
    description: str,
    version: str = "1",
) -> None:
    """The required ``Part`` base fields, in schema sequence order."""
    _text(element, "Part_number", part_number)
    _text(element, "Company_name", _UNKNOWN)
    _text(element, "Version", version)
    _text(element, "Abbreviation", abbreviation)
    _text(element, "Description", description)


def _text(parent: ET.Element, name: str, value: str) -> ET.Element:
    # KBL declares children with form="unqualified": only the root element
    # carries the namespace.
    child = ET.SubElement(parent, name)
    child.text = value
    return child


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return cleaned or "harness"
