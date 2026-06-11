"""Standard-library KiCad symbol instances for extracted components.

Annotated boxes tell a human what a region is; SYMBOL INSTANCES are what
make the document a real schematic — ERC checks pins, the netlister knows
electrical types, and the simulator binds models. KiCad ships its official
symbol library (Device, Switch, Relay, Connector_Generic, …) with every
install, so extracted components map onto those instead of reinventing
artwork:

* the used symbol definitions are EMBEDDED into the document's
  ``lib_symbols`` section (KiCad's own convention — files stay portable),
* one symbol instance is placed at each mapped component's drawing
  location, carrying the printed refDes/part number,
* short stub wires route every symbol pin to the component's traced net
  attachment points, so connectivity flows pin → wire → net and KiCad's
  ERC / SPICE netlister see a real circuit.

Components whose type has no standard-symbol mapping keep their annotation
box only — never guess a symbol.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from math import hypot
from pathlib import Path
from typing import Any

from docling_serve.extractors.kicad_sch import PT_TO_MM, _fmt
from docling_serve.extractors.spice_models import find_model

_log = logging.getLogger(__name__)

#: type-substring → (library, symbol, reference prefix). First match wins;
#: more specific tokens first. Only ELECTRICALLY faithful mappings: a
#: solenoid valve IS a coil; an unmappable type stays an annotation.
TYPE_SYMBOL_MAP: tuple[tuple[str, str, str, str], ...] = (
    ("resistor", "Device", "R", "R"),
    ("capacitor", "Device", "C", "C"),
    ("inductor", "Device", "L", "L"),
    ("coil", "Device", "L", "L"),
    ("solenoid", "Device", "L", "L"),
    ("valve", "Device", "L", "L"),
    ("led", "Device", "LED", "D"),
    ("diode", "Device", "D", "D"),
    ("transistor", "Device", "Q_NPN_BCE", "Q"),
    ("fuse", "Device", "Fuse", "F"),
    ("crystal", "Device", "Crystal", "Y"),
    ("buzzer", "Device", "Buzzer", "BZ"),
    ("speaker", "Device", "Speaker", "LS"),
    ("battery", "Device", "Battery", "BT"),
    ("switch", "Switch", "SW_SPST", "SW"),
    ("button", "Switch", "SW_Push", "SW"),
    ("relay", "Relay", "Relay_SPST-NO", "K"),
)

#: Connector types map to Connector_Generic:Conn_01xNN sized by pin count.
_CONNECTOR_TOKENS = ("connector", "plug", "terminal", "receptacle")

_PIN_BLOCK_RE = re.compile(r"\(pin\s+\w+\s+\w+", re.MULTILINE)
_AT_RE = re.compile(r"\(at\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\)")
_NUMBER_RE = re.compile(r'\(number\s+"([^"]*)"')


def find_symbol_dir() -> Path | None:
    """Locate the installed KiCad symbol libraries (no hardcoded install)."""
    candidates = [os.environ.get("KICAD_SYMBOL_DIR", "")]
    candidates += [
        str(p)
        for pattern in ("/usr/share/kicad/symbols", "/opt/kicad*/share/kicad/symbols")
        for p in sorted(Path("/").glob(pattern.lstrip("/")))
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "Device.kicad_symdir").exists():
            return Path(candidate)
    return None


class SymbolLibrary:
    """Reads official KiCad symbol definitions (split ``.kicad_symdir`` libs)."""

    def __init__(self, symbol_dir: Path) -> None:
        self._dir = symbol_dir
        self._cache: dict[str, tuple[str, list[tuple[str, float, float, float]]]] = {}

    def load(self, lib: str, name: str) -> tuple[str, list[tuple[str, float, float, float]]] | None:
        """Symbol definition (renamed ``Lib:Name``) + pins (number, x, y, angle)."""
        lib_id = f"{lib}:{name}"
        if lib_id in self._cache:
            return self._cache[lib_id]
        path = self._dir / f"{lib}.kicad_symdir" / f"{name}.kicad_sym"
        if not path.exists():
            return None
        text = path.read_text()
        block = _balanced_block(text, text.find(f'(symbol "{name}"'))
        if block is None:
            return None
        renamed = block.replace(f'(symbol "{name}"', f'(symbol "{lib_id}"', 1)
        pins = _parse_pins(block)
        self._cache[lib_id] = (renamed, pins)
        return self._cache[lib_id]


def _balanced_block(text: str, start: int) -> str | None:
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_pins(block: str) -> list[tuple[str, float, float, float]]:
    pins: list[tuple[str, float, float, float]] = []
    for match in _PIN_BLOCK_RE.finditer(block):
        pin_block = _balanced_block(block, match.start())
        if not pin_block:
            continue
        at = _AT_RE.search(pin_block)
        number = _NUMBER_RE.search(pin_block)
        if at and number:
            pins.append(
                (
                    number.group(1),
                    float(at.group(1)),
                    float(at.group(2)),
                    float(at.group(3)),
                )
            )
    return pins


def _select_symbol(
    component: dict[str, Any], attachment_count: int
) -> tuple[str, str, str] | None:
    ctype = str(component.get("type") or "").lower()
    for token, lib, name, prefix in TYPE_SYMBOL_MAP:
        if token in ctype:
            return lib, name, prefix
    if any(token in ctype for token in _CONNECTOR_TOKENS):
        pins = [p for p in component.get("pins") or [] if isinstance(p, dict)]
        count = max(len(pins), attachment_count, 2)
        count = min(count, 40)
        return "Connector_Generic", f"Conn_01x{count:02d}", "J"
    return None


def build_symbol_instances(
    graph: dict[str, Any],
    *,
    page_no: int,
    sheet_uuid: str,
    library: SymbolLibrary,
) -> tuple[dict[str, str], list[str], int]:
    """Embedded lib defs + instance/stub-wire items for one page.

    Returns ``(lib_defs by lib_id, document items, mapped component count)``.
    """
    attachments = _attachments_by_component(graph, page_no)
    used_refs: set[str] = set()
    counters: dict[str, int] = {}
    lib_defs: dict[str, str] = {}
    items: list[str] = []
    mapped = 0

    for component in graph.get("components") or []:
        if not isinstance(component, dict):
            continue
        if component.get("page") not in (None, page_no):
            continue
        bbox = component.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        points = attachments.get(str(component.get("id") or ""), [])
        selected = _select_symbol(component, len(points))
        if selected is None:
            continue
        lib, name, prefix = selected
        loaded = library.load(lib, name)
        if loaded is None:
            continue
        definition, pins = loaded
        lib_id = f"{lib}:{name}"
        lib_defs.setdefault(lib_id, definition)
        mapped += 1

        # Round placement BEFORE deriving pin/stub coordinates: KiCad's
        # connectivity engine matches wire ends to pin positions by exact
        # location, so both must be computed from the same rounded center.
        center_x = round((float(bbox[0]) + float(bbox[2])) / 2 * PT_TO_MM, 2)
        center_y = round((float(bbox[1]) + float(bbox[3])) / 2 * PT_TO_MM, 2)
        reference = _reference(component, prefix, counters, used_refs)
        value = _sim_safe_value(component, name)
        # Catalog-supplied vendor model: bind it through KiCad's Sim.*
        # properties so the simulator dialog picks it up with no clicks.
        model = find_model(
            component.get("partNumber"),
            tenant_id=str((graph.get("source") or {}).get("tenantId") or "") or None,
        )
        sim_properties: list[tuple[str, str]] = []
        if model is not None and model.is_subckt:
            sim_properties = [
                ("Sim.Library", str(model.path)),
                ("Sim.Name", model.name),
                ("Sim.Device", "SUBCKT"),
            ]
        items.append(
            _instance_sexpr(
                lib_id,
                reference=reference,
                value=value,
                x=center_x,
                y=center_y,
                pins=pins,
                sheet_uuid=sheet_uuid,
                extra_properties=sim_properties,
            )
        )

        # Stub wires: each symbol pin to its nearest unclaimed attachment
        # point, so net connectivity reaches the pin and ERC sees a circuit.
        # Symbol-local Y points up; schematic Y points down (KiCad placement
        # rule: world = (cx + px, cy - py) at rotation 0).
        remaining = [(float(px), float(py)) for px, py in points]
        for _number, pin_x, pin_y, _angle in pins:
            world_x = center_x + pin_x
            world_y = center_y - pin_y
            if not remaining:
                # No traced wire reaches this pin — declare it intentionally
                # open with a no-connect marker, the schematic-standard way to
                # tell ERC "this is known", instead of leaving a violation.
                items.append(
                    f"  (no_connect (at {_fmt(world_x)} {_fmt(world_y)}) "
                    f'(uuid "{uuid.uuid4()}"))'
                )
                continue
            nearest = min(
                range(len(remaining)),
                key=lambda i: hypot(
                    remaining[i][0] * PT_TO_MM - world_x,
                    remaining[i][1] * PT_TO_MM - world_y,
                ),
            )
            att_x, att_y = remaining.pop(nearest)
            items.append(
                f"  (wire (pts (xy {_fmt(world_x)} {_fmt(world_y)}) "
                f"(xy {_fmt(att_x * PT_TO_MM)} {_fmt(att_y * PT_TO_MM)})) "
                f'(stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))'
            )
    return lib_defs, items, mapped


def _attachments_by_component(
    graph: dict[str, Any], page_no: int
) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for net in graph.get("nets") or []:
        if not isinstance(net, dict) or net.get("page") not in (None, page_no):
            continue
        for node in net.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            attachment = node.get("attachment")
            comp_id = str(node.get("component") or "")
            if comp_id and isinstance(attachment, (list, tuple)) and len(attachment) == 2:
                out.setdefault(comp_id, []).append(
                    (float(attachment[0]), float(attachment[1]))
                )
    return out


#: KiCad-legal reference: letter prefix + number, nothing else (annotation
#: requires it; spaces, dashes, or gate suffixes break netlisting).
_LEGAL_REF_RE = re.compile(r"^[A-Za-z]+\d+$")


def _reference(
    component: dict[str, Any],
    prefix: str,
    counters: dict[str, int],
    used: set[str],
) -> str:
    ref = str(component.get("refDes") or "").strip()
    if ref and _LEGAL_REF_RE.match(ref) and ref.upper() not in used:
        used.add(ref.upper())
        return ref
    while True:
        counters[prefix] = counters.get(prefix, 0) + 1
        candidate = f"{prefix}{counters[prefix]}"
        if candidate.upper() not in used:
            used.add(candidate.upper())
            return candidate


_VALUE_TOKEN_RE = re.compile(r"^\d+(\.\d+)?\s*[a-zA-ZµΩ]{0,4}$")


def _sim_safe_value(component: dict[str, Any], symbol_name: str) -> str:
    """A Value KiCad's SPICE netlister can emit verbatim.

    The Value field flows into the SPICE element line, so free text
    ("capacitor (detected #17)") breaks simulation. Printed component
    values (10k, 0.1u) pass through; otherwise the part number with
    whitespace collapsed; otherwise the symbol name.
    """
    raw = str(component.get("value") or "").strip()
    if _VALUE_TOKEN_RE.match(raw):
        return raw
    part = str(component.get("partNumber") or "").strip()
    if part:
        return re.sub(r"\s+", "_", part)
    return symbol_name


def _instance_sexpr(
    lib_id: str,
    *,
    reference: str,
    value: str,
    x: float,
    y: float,
    pins: list[tuple[str, float, float, float]],
    sheet_uuid: str,
    extra_properties: list[tuple[str, str]] | None = None,
) -> str:
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    pin_lines = "\n".join(
        f'    (pin "{esc(number)}" (uuid "{uuid.uuid4()}"))'
        for number, _px, _py, _angle in pins
    )
    extra_lines = "".join(
        f'    (property "{esc(key)}" "{esc(val)}" (at {_fmt(x)} {_fmt(y)} 0)\n'
        f"      (effects (font (size 1.27 1.27)) (hide yes)))\n"
        for key, val in extra_properties or []
    )
    return (
        f'  (symbol (lib_id "{esc(lib_id)}") (at {_fmt(x)} {_fmt(y)} 0) (unit 1)\n'
        f"    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"
        f'    (uuid "{uuid.uuid4()}")\n'
        f'    (property "Reference" "{esc(reference)}" (at {_fmt(x)} {_fmt(y - 3)} 0)\n'
        f"      (effects (font (size 1.27 1.27))))\n"
        f'    (property "Value" "{esc(value)}" (at {_fmt(x)} {_fmt(y + 3)} 0)\n'
        f"      (effects (font (size 1.27 1.27))))\n"
        f'    (property "Footprint" "" (at {_fmt(x)} {_fmt(y)} 0)\n'
        f"      (effects (font (size 1.27 1.27)) (hide yes)))\n"
        f'    (property "Datasheet" "" (at {_fmt(x)} {_fmt(y)} 0)\n'
        f"      (effects (font (size 1.27 1.27)) (hide yes)))\n"
        f"{extra_lines}"
        f"{pin_lines}\n"
        f'    (instances (project "" (path "/{sheet_uuid}" '
        f'(reference "{esc(reference)}") (unit 1))))\n'
        f"  )"
    )


def embed_lib_symbols(kicad_text: str, lib_defs: dict[str, str]) -> str:
    """Embed used library symbol definitions into ``(lib_symbols)``."""
    if not lib_defs:
        return kicad_text
    body = "\n".join(_indent(defn, "    ") for defn in lib_defs.values())
    return kicad_text.replace("  (lib_symbols)", f"  (lib_symbols\n{body}\n  )", 1)


def _indent(block: str, pad: str) -> str:
    return "\n".join(pad + line for line in block.splitlines())


def document_sheet_uuid(kicad_text: str) -> str:
    match = re.search(r'\(uuid\s+"([0-9a-f-]+)"\)', kicad_text)
    return match.group(1) if match else str(uuid.uuid4())
