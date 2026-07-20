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
    # Calibrate the page confidence against STRUCTURE: the model's own score
    # ignores how many of its components carry no drawing evidence (printed
    # identity or a traced wire) — the review-measured failure mode where a
    # quarter of emitted parts did not exist yet confidence said 0.92.
    verified = 0
    considered = 0
    attached_ids = {
        str(node.get("component"))
        for net in graph.get("nets") or []
        for node in net.get("nodes") or []
        if isinstance(node, dict) and node.get("attachment")
    }
    for component in components.values():
        considered += 1
        has_identity = any(
            str(component.get(field) or "").strip() for field in ("value", "partNumber")
        )
        # An off-page terminal is self-evidencing: its printed text IS its
        # identity, so it counts as verified without a value/part number.
        is_offpage = "off-page" in str(component.get("type") or "").lower()
        if has_identity or is_offpage or str(component.get("id")) in attached_ids:
            verified += 1
    if considered:
        from docling_serve.schematic.schematic_tuning import TUNING

        fraction = verified / considered
        quality["verifiedComponentFraction"] = round(fraction, 3)
        base = graph.get("confidence")
        if isinstance(base, (int, float)):
            graph["confidenceCalibrated"] = round(
                float(base) * (0.5 + 0.5 * fraction), 2
            )
        # Confidence GATE: below the evidence threshold the extraction is not
        # trustworthy enough to publish as-is — flag it for human review
        # instead of letting a passing SPICE run imply correctness.
        quality["needsReview"] = fraction < TUNING.min_verified_fraction_gate
    graph["connectivityQuality"] = quality
    return quality


#: Word-level aliases applied before token matching (drawing shorthand).
_TOKEN_ALIASES = {
    "sig": "signal",
    "gnd": "ground",
    "rtn": "return",
    "exc": "excitation",
}


def _match_tokens(text: str) -> set[str]:
    import re as _re

    tokens = []
    for raw in _re.split(r"[^A-Za-z0-9+±-]+", text.lower()):
        token = raw.strip("_")
        if not token:
            continue
        token = _TOKEN_ALIASES.get(token, token)
        # "pin8" and "pin 8" must compare equal.
        tokens.extend(_re.findall(r"[a-z±+-]+|\d+", token) or [token])
    return {t for t in tokens if t not in ("on", "to", "the", "of", "a", "an")}


#: Component families whose printed VALUES ("1K", "100n") get mis-detected as
#: separate components by the detection pass.
_VALUE_ECHO_FAMILIES = ("resistor", "capacitor", "inductor")


def drop_value_text_echoes(graph: dict[str, Any]) -> list[str]:
    """Remove floating components that are printed-value text mis-detections.

    Signature: a FLOATING component with no printed value of its own whose
    refDes exactly equals the VALUE of an attached component of the same
    family ("1K" resistor next to R1 value=1K) — the detection pass boxed
    the value text as if it were a second part. Deterministic; anything less
    exact stays on the QA worklist for a human. Returns removal notes.
    """
    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    member_ids = {
        str(node.get("component"))
        for net in graph.get("nets") or []
        for node in net.get("nodes") or []
        if isinstance(node, dict) and node.get("component")
    }

    def family(component: dict[str, Any]) -> str | None:
        ctype = str(component.get("type") or "").lower()
        return next((f for f in _VALUE_ECHO_FAMILIES if f in ctype), None)

    attached_values: dict[tuple[str, str], str] = {}
    for component in components:
        fam = family(component)
        value = str(component.get("value") or "").strip().lower()
        if fam and value and str(component.get("id")) in member_ids:
            attached_values.setdefault(
                (value, fam), str(component.get("refDes") or component.get("id"))
            )

    notes: list[str] = []
    kept: list[dict[str, Any]] = []
    for component in components:
        comp_id = str(component.get("id"))
        ref = str(component.get("refDes") or "").strip().lower()
        fam = family(component)
        if (
            comp_id not in member_ids
            and ref
            and fam
            and not str(component.get("value") or "").strip()
            and (ref, fam) in attached_values
        ):
            owner = attached_values[(ref, fam)]
            notes.append(
                f"dropped {component.get('refDes')} ({comp_id}): "
                f"printed value of {owner} boxed as a component"
            )
            continue
        kept.append(component)
    if notes:
        graph["components"] = kept
    return notes


def drop_quantity_annotations(graph: dict[str, Any]) -> list[str]:
    """Remove components that are quantity annotations, not parts.

    A parenthesized numeral — ``(2)`` beside a symbol — means "two of these"
    on engineering drawings. A component whose only identity is such a
    numeral (refDes like ``(2)``, no part number, no printed description
    beyond it) is the annotation itself boxed as a part. Returns notes.
    """
    import re as _re

    pattern = _re.compile(r"^\(\d+\)$")
    notes: list[str] = []
    kept: list[dict[str, Any]] = []
    dropped_ids: set[str] = set()
    for component in graph.get("components") or []:
        if not isinstance(component, dict):
            continue
        ref = str(component.get("refDes") or "").strip()
        if pattern.match(ref) and not str(component.get("partNumber") or "").strip():
            dropped_ids.add(str(component.get("id")))
            notes.append(f"dropped {ref} ({component.get('id')}): quantity annotation")
            continue
        kept.append(component)
    if not notes:
        return []
    graph["components"] = kept
    for net in graph.get("nets") or []:
        if isinstance(net, dict):
            net["nodes"] = [
                node
                for node in net.get("nodes") or []
                if not (
                    isinstance(node, dict) and str(node.get("component")) in dropped_ids
                )
            ]
    return notes


def normalize_quantity_values(graph: dict[str, Any]) -> int:
    """Move quantity annotations out of ``value`` into ``quantity``, in place.

    ``(2)`` beside a symbol means "two of these"; the vision pass records it
    on the symbol as ``value: "2"``. A quantity is not a value — exporters
    would print it as a rating — so it moves to a first-class ``quantity``
    attribute. Parenthesized numerals convert on any component; bare small
    integers convert only on grounds (which never carry a value). Returns
    how many components were normalized.
    """
    import re as _re

    # "(2)" anywhere; a bare small integer only on value-less families.
    quantity_re = _re.compile(r"^\((\d{1,2})\)$|^(\d{1,2})$")
    normalized = 0
    for component in graph.get("components") or []:
        if not isinstance(component, dict):
            continue
        value = str(component.get("value") or "").strip()
        if not value:
            continue
        match = quantity_re.match(value)
        if not match:
            continue
        is_parenthesized = match.group(1) is not None
        is_ground = "ground" in str(component.get("type") or "").lower()
        if not (is_parenthesized or is_ground):
            continue
        component["quantity"] = int(match.group(1) or match.group(2))
        component["value"] = None
        normalized += 1
    return normalized


def _bbox_iou(a: Any, b: Any) -> float:
    if not (
        isinstance(a, (list, tuple))
        and len(a) == 4
        and isinstance(b, (list, tuple))
        and len(b) == 4
    ):
        return 0.0
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def merge_duplicate_detections(graph: dict[str, Any]) -> list[str]:
    """Reconcile the model and detection passes: one glyph, one component.

    The whole-page pass and the detection-only pass routinely box the same
    symbol at slightly different coordinates ("capacitor (detected #1)" and
    the model's own component are ONE glyph). A detection-origin component
    (description ``… (detected #n)`` or no identity) whose box OVERLAPS a
    richer component's box by :data:`_DUPLICATE_MERGE_IOU` merges into it:
    memberships transfer, the echo drops. IoU overlap (not center-inside)
    ensures a small symbol contained in a large enclosure box is never
    swallowed by it. Returns notes.
    """
    import re as _re

    from docling_serve.schematic.schematic_tuning import TUNING

    merge_iou = TUNING.duplicate_merge_iou
    detected_re = _re.compile(r"\(detected #\d+\)\s*$")
    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]

    def is_echo(component: dict[str, Any]) -> bool:
        if detected_re.search(str(component.get("description") or "")):
            return not str(component.get("partNumber") or "").strip()
        return not any(
            str(component.get(field) or "").strip()
            for field in ("refDes", "value", "partNumber", "description")
        )

    rich = [c for c in components if not is_echo(c)]
    merged_into: dict[str, str] = {}
    notes: list[str] = []
    kept: list[dict[str, Any]] = []
    for component in components:
        bbox = component.get("bbox")
        if not (is_echo(component) and bbox):
            kept.append(component)
            continue
        best = max(
            rich,
            key=lambda r: _bbox_iou(bbox, r.get("bbox")),
            default=None,
        )
        if best is None or _bbox_iou(bbox, best.get("bbox")) < merge_iou:
            kept.append(component)
            continue
        host = best
        merged_into[str(component.get("id"))] = str(host.get("id"))
        notes.append(
            f"merged {component.get('id')} ({str(component.get('description'))[:30]})"
            f" into {host.get('refDes') or host.get('id')}"
        )
    if not notes:
        return []
    graph["components"] = kept
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        seen: set[str] = set()
        nodes = []
        for node in net.get("nodes") or []:
            if isinstance(node, dict) and node.get("component"):
                comp_id = merged_into.get(
                    str(node["component"]), str(node["component"])
                )
                node["component"] = comp_id
                key = f"{comp_id}:{node.get('pin')}:{node.get('attachment')}"
                if key in seen:
                    continue
                seen.add(key)
            nodes.append(node)
        net["nodes"] = nodes
    return notes


def mark_ground_nets(graph: dict[str, Any]) -> int:
    """Stamp ``class: ground`` on nets a ground SYMBOL physically touches.

    The drawing's ground glyph IS the net's classification; recording it on
    the graph makes every consumer agree — the SPICE emitter collapses the
    net to node 0, KiCad labels it GND, EEvision types the wire ground.
    Returns how many nets were classified.
    """
    ground_components = {
        str(c.get("id"))
        for c in graph.get("components") or []
        if isinstance(c, dict) and "ground" in str(c.get("type") or "").lower()
    }
    marked = 0
    for net in graph.get("nets") or []:
        if not isinstance(net, dict) or net.get("class"):
            continue
        touched = {
            str(node.get("component"))
            for node in net.get("nodes") or []
            if isinstance(node, dict) and node.get("component")
        }
        if touched & ground_components:
            net["class"] = "ground"
            marked += 1
    return marked


def reattach_floating_components(graph: dict[str, Any]) -> list[str]:
    """Attach zero-membership components to nets the extraction itself names.

    A scanned drawing's tracer can miss a component's wire stub, leaving the
    component detected but floating. Two EVIDENCE-BACKED repairs (never a
    guess) reconnect it:

    * **Text evidence** — the vision pass wrote the net into the component's
      own description or refDes ("Filter capacitor on +13 VDC output",
      connector refDes "26 VAC"): if exactly one net's name tokens are a
      subset of that text's tokens, the component joins that net.
    * **Geometry evidence** — a traced wire segment ENDS on/inside the
      component's bbox: that wire was clipped at this component, so the
      component joins its net (the tracer's own touch rule).

    Memberships gain ``membershipSource`` provenance and land on the QA
    worklist unpinned. Ambiguity (multiple candidate nets) attaches nothing.
    Returns human-readable notes of what was reattached.
    """
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]
    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    member_ids = {
        str(node.get("component"))
        for net in nets
        for node in net.get("nodes") or []
        if isinstance(node, dict) and node.get("component")
    }
    named_nets = [
        (net, _match_tokens(str(net.get("name")))) for net in nets if net.get("name")
    ]

    notes: list[str] = []
    for component in components:
        comp_id = str(component.get("id") or "")
        if not comp_id or comp_id in member_ids:
            continue

        evidence = " ".join(
            str(v) for v in (component.get("description"), component.get("refDes")) if v
        )
        attached_net = None
        source = None
        if evidence:
            evidence_tokens = _match_tokens(evidence)
            candidates = [
                (net, tokens)
                for net, tokens in named_nets
                if tokens and tokens <= evidence_tokens
            ]
            if candidates:
                # Prefer the most specific (longest) matching name; a tie
                # between different nets is ambiguous — attach nothing.
                candidates.sort(key=lambda item: -len(item[1]))
                if len(candidates) == 1 or len(candidates[0][1]) > len(
                    candidates[1][1]
                ):
                    attached_net = candidates[0][0]
                    source = "description-inference"
            elif len(evidence_tokens) >= 2 and _is_connector(component):
                # The reverse direction, for CONNECTORS only: a connector
                # named after the wire it terminates ("SYNCHRO EXC") matches
                # a net whose fuller printed name contains it ("26 VAC TO
                # SYNCHRO EXC"). Terminating a run is what connectors DO, so
                # subset-of-net-name is safe where it would be a guess for
                # any other component class.
                containing = [
                    net for net, tokens in named_nets if evidence_tokens <= tokens
                ]
                if len(containing) == 1:
                    attached_net = containing[0]
                    source = "description-inference"

        if attached_net is None:
            hit = _segment_ending_in_bbox(component, nets)
            if hit is not None:
                attached_net, point = hit
                source = "segment-terminus"

        if attached_net is None:
            continue
        node: dict[str, Any] = {
            "component": comp_id,
            "pin": None,
            "membershipSource": source,
        }
        if source == "segment-terminus":
            node["attachment"] = [round(point[0], 1), round(point[1], 1)]
        attached_net.setdefault("nodes", []).append(node)
        member_ids.add(comp_id)
        label = component.get("refDes") or comp_id
        target = attached_net.get("name") or attached_net.get("id")
        notes.append(f"reattached {label} -> {target} ({source})")
    return notes


def _segment_ending_in_bbox(
    component: dict[str, Any], nets: list[dict[str, Any]]
) -> tuple[dict[str, Any], tuple[float, float]] | None:
    """The unique net whose traced wire terminates at this component."""
    bbox = component.get("bbox")
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        return None
    from docling_serve.schematic.schematic_tuning import TUNING

    page = component.get("page")
    x0, y0, x1, y1 = (float(v) for v in bbox)
    tol = TUNING.bbox_attach_tolerance_pt
    hits: list[tuple[dict[str, Any], tuple[float, float]]] = []
    for net in nets:
        if page is not None and net.get("page") not in (None, page):
            continue
        for segment in net.get("segments") or []:
            if not (isinstance(segment, (list, tuple)) and len(segment) == 4):
                continue
            for px, py in ((segment[0], segment[1]), (segment[2], segment[3])):
                if (x0 - tol) <= float(px) <= (x1 + tol) and (y0 - tol) <= float(
                    py
                ) <= (y1 + tol):
                    hits.append((net, (float(px), float(py))))
                    break
            else:
                continue
            break
    distinct = {id(net) for net, _ in hits}
    if len(distinct) != 1:
        return None  # nothing terminates here, or it's ambiguous
    return hits[0]


def _is_connector(component: dict[str, Any]) -> bool:
    ctype = str(component.get("type") or "").lower()
    return any(
        token in ctype for token in ("connector", "plug", "terminal", "receptacle")
    )


def _is_two_terminal(component: dict[str, Any]) -> bool:
    ctype = str(component.get("type") or "").lower()
    return any(token in ctype for token in TWO_TERMINAL_TOKENS)


def _attachment_order(node: dict[str, Any]) -> tuple[float, float]:
    attachment = node.get("attachment")
    if isinstance(attachment, (list, tuple)) and len(attachment) == 2:
        return float(attachment[0]), float(attachment[1])
    return (float("inf"), float("inf"))
