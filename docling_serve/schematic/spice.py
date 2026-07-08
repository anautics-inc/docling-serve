"""Render a Captify schematic graph as a generic SPICE netlist (.cir).

SPICE is the one simulation-capable format shared across the toolchains we
target: KiCad imports and simulates it natively (bundled ngspice), and the
Altair EDA family EEvision belongs to reads it through SpiceVision /
StarVision PRO (generic SPICE plus the commercial dialects). Emitting plain,
dialect-free SPICE keeps every door open.

Mapping from ``captify.schematic.v1``:

* nets → SPICE nodes (sanitized printed names — A8B22 stays A8B22),
* passives with a recognizable type → native primitives (R/C/L) so a
  simulator can elaborate them directly,
* everything else (valves, relays, ICs, switches) → ``X`` subcircuit
  instances against generated ``.subckt`` stubs that carry the printed part
  number and pin count — simulation-ready scaffolding a user fills with real
  models, and exactly the shape SpiceVision renders as schematic symbols.

Like the other serializers, this is pure and deterministic; the test suite
parses the output with ngspice in batch mode when it is installed.
"""

from __future__ import annotations

import re
from typing import Any

#: SPICE element prefixes by (lower-cased substring of) component type.
_PRIMITIVES = (
    ("resistor", "R"),
    ("capacitor", "C"),
    ("inductor", "L"),
)

#: Placeholder values for primitives whose printed value the drawing omits
#: (or OCR missed). Type-typical orders of magnitude — a bare "C" must never
#: become 1 FARAD. Every use is flagged with an ``* ASSUMED`` comment.
_PRIMITIVE_DEFAULTS = {"R": "10k", "C": "100n", "L": "1m"}

_NODE_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]")

#: Ground semantics: net names / component types that mean "this is the
#: reference node". Mirrors the simulation module's detection (which imports
#: these) so the emitted netlist and the stimulus builder can never disagree.
GROUND_NAME_RE = re.compile(
    r"^(GND|GROUND|VSS|CHASSIS|EARTH|COM|COMMON|0V|RETURN|RTN|SIG\s*GND|CHASSIS\s*GROUND)$",
    re.IGNORECASE,
)
#: MIL-W-5088 aircraft wire codes: the trailing N segment marks a ground wire.
MIL_GROUND_WIRE_RE = re.compile(r"^[A-Z0-9]+N$", re.IGNORECASE)
_GROUND_TYPE_RE = re.compile(r"\b(ground|gnd|chassis|earth)\b", re.IGNORECASE)


def is_ground_net(net: dict[str, Any]) -> bool:
    """Does this net's name/class mark it as the reference (ground) net?"""
    if str(net.get("class") or "").strip().lower() == "ground":
        return True
    if str(net.get("signalType") or "").strip().lower() == "ground":
        return True
    for candidate in (net.get("name"), net.get("wireId")):
        text = str(candidate or "").strip()
        if text and (GROUND_NAME_RE.match(text) or MIL_GROUND_WIRE_RE.match(text)):
            return True
    return False


def is_ground_component(component: dict[str, Any]) -> bool:
    """Is this a ground SYMBOL (chassis/earth/signal ground glyph)?"""
    return bool(_GROUND_TYPE_RE.search(str(component.get("type") or "")))


def ground_net_ids(graph: dict[str, Any]) -> set[str]:
    """Net ids that must collapse to SPICE node 0.

    A net is ground when its own labels say so (:func:`is_ground_net`) or when
    a ground SYMBOL touches it — the drawing's way of tying a wire to the
    reference. Both are structural facts, not simulation guesses.
    """
    ground_components = {
        str(c.get("id"))
        for c in graph.get("components") or []
        if isinstance(c, dict) and is_ground_component(c)
    }
    out: set[str] = set()
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        touched = {
            str(m.get("component"))
            for m in net.get("nodes") or []
            if isinstance(m, dict) and m.get("component")
        }
        if is_ground_net(net) or (touched & ground_components):
            out.add(str(net.get("id") or ""))
    return out


def graph_to_spice(graph: dict[str, Any], *, source_name: str) -> str:
    """Serialize a ``captify.schematic.v1`` graph to a SPICE netlist string.

    Model resolution per component, in order: catalog/vendor model →
    inferred first-order physics (``spice_inference``) → connectivity stub.

    Ground semantics are structural: nets labeled as ground (or touched by a
    ground symbol) become SPICE node 0, and ground symbols themselves are not
    emitted as components. Components with no traced net membership at all
    are omitted (with an audit comment) — floating elements only poison the
    matrix and can never affect a solve.
    """
    from docling_serve.schematic.spice_inference import (
        InferredBody,
        infer_subckt_body,
        model_cards_for,
    )
    from docling_serve.schematic.spice_models import find_model

    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]
    tenant_id = _graph_tenant(graph)

    node_by_membership = _node_assignments(nets, node_names=net_node_names(graph))

    lines: list[str] = [
        f"* {graph.get('titleBlock', {}).get('title') or source_name}".strip(),
        f"* Generated by captify-docling-serve schematic extractor from {source_name}",
        "* Generic SPICE netlist: vendor models bind from the model library;",
        "* INFERRED subcircuits carry first-order physics derived from the",
        "* component type; remaining stubs await vendor models.",
        "",
    ]

    subckts: dict[str, int] = {}  # stub subckt name -> pin count
    inferred: dict[str, tuple[int, InferredBody]] = {}  # subckt -> (pins, body)
    bound_models: dict[str, str] = {}  # model name -> model text
    primitive_wrappers: dict[str, tuple[int, str]] = {}  # subckt -> (pins, body)
    element_names: set[str] = set()
    for index, component in enumerate(components, start=1):
        comp_id = str(component.get("id") or f"C{index:04d}")
        ref = _sanitize(str(component.get("refDes") or comp_id))
        ctype = str(component.get("type") or "").lower()
        nodes = node_by_membership.get(comp_id) or []

        # Ground symbols are node-0 semantics, not devices: their nets were
        # already collapsed to 0 by _node_assignments, so emitting them as
        # elements would only add fake 2-pin bodies dangling off the reference.
        if is_ground_component(component):
            continue

        # A component with NO traced net membership can never carry current;
        # emitted anyway it sits on floating NC_* nodes and makes the matrix
        # singular. Omit it, but leave an audit trail in the netlist.
        if not nodes:
            lines.append(
                f"* OMITTED {ref}: no traced net membership (component floats"
                " — see the extraction QA worklist)"
            )
            continue

        pins = [p for p in component.get("pins") or [] if isinstance(p, dict)]
        pin_count = max(len(nodes), len(pins), 2)

        # A catalog-supplied vendor model fixes the TRUE pin count; the
        # instance binds it directly and becomes genuinely simulatable.
        model = find_model(component.get("partNumber"), tenant_id=tenant_id)
        if model is not None and model.is_subckt:
            pin_count = model.pin_count

        nodes = (nodes + [f"NC_{ref}_{i}" for i in range(len(nodes), pin_count)])[
            :pin_count
        ]

        if model is not None and model.is_subckt:
            bound_models[model.name] = model.text
            name = _unique(f"X{ref}", element_names, "X")
            lines.append(f"{name} {' '.join(nodes)} {model.name}")
            continue

        # Vendor ``.model`` card (primitive device, not a subcircuit): wrap it
        # in a generated subckt instantiating the right SPICE element, when
        # the traced pin count matches the device's terminals.
        if model is not None:
            wrapper = _primitive_wrapper(model, pin_count)
            if wrapper is not None:
                wrapper_name, body = wrapper
                bound_models[model.name] = model.text
                primitive_wrappers[wrapper_name] = (pin_count, body)
                name = _unique(f"X{ref}", element_names, "X")
                lines.append(f"{name} {' '.join(nodes)} {wrapper_name}")
                continue

        prefix = next(
            (p for token, p in _PRIMITIVES if token in ctype), None
        )
        if prefix and pin_count == 2:
            name = _unique(f"{prefix}{ref}", element_names, prefix)
            value, assumed = _component_value(component, prefix)
            if assumed:
                lines.append(
                    f"* ASSUMED {name} value {value} (drawing prints no value)"
                )
            lines.append(f"{name} {nodes[0]} {nodes[1]} {value}")
            continue

        # Inferred tier: first-order physics from the component type when no
        # vendor model exists (relay coil, switch contact, lamp filament, …).
        body = infer_subckt_body(component, pin_count)
        if body is not None:
            base = f"INF_{_subckt_name(component, comp_id)[3:]}_{pin_count}P"
            # Same name but different internals (two lamps with different
            # printed values) must not share a definition.
            subckt, suffix = base, 2
            while subckt in inferred and inferred[subckt][1] != body:
                subckt = f"{base}_{suffix}"
                suffix += 1
            inferred[subckt] = (pin_count, body)
            name = _unique(f"X{ref}", element_names, "X")
            lines.append(f"{name} {' '.join(nodes)} {subckt}")
            continue

        # Arity-specific stub names: two occurrences of one part number can
        # surface with different pin counts (a 7-pin booster vs its 4-pin
        # sibling) and SPICE requires instance nodes to match the .subckt.
        subckt = f"{_subckt_name(component, comp_id)}_{pin_count}P"
        subckts[subckt] = pin_count
        name = _unique(f"X{ref}", element_names, "X")
        lines.append(f"{name} {' '.join(nodes)} {subckt}")

    lines.extend(
        _definition_sections(
            bound_models, primitive_wrappers, inferred, subckts, model_cards_for
        )
    )
    lines.append("")
    lines.append(".end")
    return "\n".join(lines) + "\n"


def _definition_sections(
    bound_models: dict[str, str],
    primitive_wrappers: dict[str, tuple[int, str]],
    inferred: dict[str, tuple[int, Any]],
    subckts: dict[str, int],
    model_cards_for: Any,
) -> list[str]:
    """The netlist's definition trailer: vendor models, wrappers, inferred
    bodies, and connectivity stubs (in binding-priority order)."""

    def pin_list(count: int) -> str:
        return " ".join(f"p{i + 1}" for i in range(count))

    lines: list[str] = []
    if bound_models:
        lines.append("")
        lines.append("* vendor models resolved from the SPICE model library")
        lines.extend(bound_models.values())

    if primitive_wrappers:
        lines.append("")
        lines.append("* wrappers binding vendor .model cards to traced pins")
        for wrapper_name, (pin_total, body) in sorted(primitive_wrappers.items()):
            lines.append(f".subckt {wrapper_name} {pin_list(pin_total)}")
            lines.append(body)
            lines.append(".ends")

    if inferred:
        lines.append("")
        lines.append("* INFERRED first-order models (derived from component type,")
        lines.append("* not vendor data) - replace with vendor models when available")
        for subckt, (pin_count, body) in sorted(inferred.items()):
            lines.append(f".subckt {subckt} {pin_list(pin_count)}")
            lines.append(f"* INFERRED: {body.rationale}")
            lines.extend(body.lines)
            lines.append(".ends")
        lines.extend(model_cards_for([body for _, body in inferred.values()]))

    if subckts:
        lines.append("")
        for subckt, pin_count in sorted(subckts.items()):
            lines.append(f".subckt {subckt} {pin_list(pin_count)}")
            lines.append("* stub - substitute the vendor model for simulation")
            # A placeholder element keeps instances alive through simulator
            # elaboration (empty subcircuits are silently optimized away) and
            # makes the netlist runnable as-is for connectivity checks.
            lines.append(f"Rstub p1 p{pin_count} 1G")
            lines.append(".ends")
    return lines


#: Wrapper recipes for vendor ``.model`` device types: terminals expected and
#: the element line instantiating the model inside the generated subckt.
_PRIMITIVE_WRAPPERS: dict[str, tuple[int, str]] = {
    "D": (2, "D1 p1 p2 {name}"),
    "NPN": (3, "Q1 p1 p2 p3 {name}"),
    "PNP": (3, "Q1 p1 p2 p3 {name}"),
    # Bulk tied to source: drawings trace 3 MOSFET terminals.
    "NMOS": (3, "M1 p1 p2 p3 p3 {name}"),
    "PMOS": (3, "M1 p1 p2 p3 p3 {name}"),
    "NJF": (3, "J1 p1 p2 p3 {name}"),
    "PJF": (3, "J1 p1 p2 p3 {name}"),
    "R": (2, "R1 p1 p2 {name}"),
    "C": (2, "C1 p1 p2 {name}"),
    "L": (2, "L1 p1 p2 {name}"),
}


def _primitive_wrapper(model: Any, pin_count: int) -> tuple[str, str] | None:
    """A ``(subckt_name, body)`` wrapper for a vendor ``.model`` card, or None.

    Only binds when the traced pin count matches the device's terminal count —
    anything else would silently mis-wire, so it falls through to inference.
    """
    recipe = _PRIMITIVE_WRAPPERS.get(str(getattr(model, "model_type", "")).upper())
    if recipe is None:
        return None
    terminals, template = recipe
    if pin_count != terminals:
        return None
    wrapper_name = f"MDL_{_sanitize(model.name).upper()}_{terminals}P"
    return wrapper_name, template.format(name=model.name)


def _graph_tenant(graph: dict[str, Any]) -> str | None:
    """Tenant carried on the graph's source — scopes model-library lookups."""
    tenant = (graph.get("source") or {}).get("tenantId")
    return str(tenant) if tenant else None


def net_node_names(graph: dict[str, Any]) -> dict[str, str]:
    """Net id -> the SPICE node name the emitter assigns it.

    THE authoritative mapping: ground nets (see :func:`ground_net_ids`) are
    the literal reference node ``0``; every other net gets its sanitized
    printed name, uniquified. The simulation stimulus builder uses this same
    function, so sources always land on the emitted node names.
    """
    grounded = ground_net_ids(graph)
    names: dict[str, str] = {}
    used_names: set[str] = {"0"}
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]
    for index, net in enumerate(nets, start=1):
        net_id = str(net.get("id") or "")
        if net_id in grounded:
            names[net_id] = "0"
            continue
        raw = net.get("name") or net.get("wireId") or f"N{index:03d}"
        names[net_id] = _unique(sanitize_node(str(raw)), used_names, "N")
    return names


def _node_assignments(
    nets: list[dict[str, Any]], *, node_names: dict[str, str]
) -> dict[str, list[str]]:
    """Component id -> ordered SPICE node names from net memberships."""
    out: dict[str, list[str]] = {}
    for net in nets:
        node = node_names.get(str(net.get("id") or ""))
        if node is None:
            continue
        for member in net.get("nodes") or []:
            if isinstance(member, dict) and member.get("component"):
                out.setdefault(str(member["component"]), []).append(node)
    return out


def _component_value(component: dict[str, Any], prefix: str) -> tuple[str, bool]:
    """Best-effort primitive value as ``(value, assumed)``.

    When the drawing prints no value, returns the type-typical placeholder
    from :data:`_PRIMITIVE_DEFAULTS` with ``assumed=True`` so the emitter can
    flag it — silently defaulting to ``1`` meant 1 FARAD for a bare cap.
    """
    value = str(component.get("value") or "").strip()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([kKmMuUnNpPfFgG]?)", value)
    if match:
        return match.group(1) + match.group(2).lower(), False
    return _PRIMITIVE_DEFAULTS.get(prefix, "1"), True


def _subckt_name(component: dict[str, Any], fallback: str) -> str:
    base = component.get("partNumber") or component.get("type") or fallback
    return "SC_" + _sanitize(str(base)).upper()


def sanitize_node(text: str) -> str:
    """SPICE-legal node/ref name that PRESERVES polarity signs.

    ``+13 VDC`` and ``-13 VDC`` are different rails; the old strip-everything
    sanitizer collapsed both to ``13_VDC`` and silently resolved the collision
    with a ``_2`` suffix — polarity lost, model wrong-but-passing. Leading and
    trailing signs (``B+`` / ``B-``) are encoded as ``P``/``N`` markers.
    """
    t = text.strip()
    prefix = ""
    if t.startswith("+"):
        prefix, t = "P", t[1:]
    elif t.startswith("-"):
        prefix, t = "N", t[1:]
    suffix = ""
    if t.endswith("+"):
        suffix, t = "_P", t[:-1]
    elif t.endswith("-"):
        suffix, t = "_N", t[:-1]
    cleaned = _NODE_SANITIZE_RE.sub("_", t).strip("_")
    combined = f"{prefix}{cleaned}{suffix}".strip("_")
    return combined or "X"


def _sanitize(text: str) -> str:
    return sanitize_node(text)


def _unique(name: str, used: set[str], prefix: str) -> str:
    candidate = name
    counter = 2
    while candidate.upper() in {u.upper() for u in used}:
        candidate = f"{name}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate
