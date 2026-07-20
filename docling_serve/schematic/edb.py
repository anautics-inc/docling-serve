"""Build a ready-to-load Altair EEvision ``.edb`` from a Captify schematic graph.

EEvision's native database is written through the vendor's EDB Creator API
(``PedbCreator.py``, a Python wrapper over their C library) — there is no
open ``.edb`` file format, so this module drives that API directly instead of
going through the licensed ``edml2edb`` compiler. The wrapper ships with the
EEvision installation; point ``EEVISION_PEDB_DIR`` at the directory holding
``PedbCreator.py`` (and its native library) to enable ``.edb`` emission.

The mapping is the SAME shared one the EDML/CSV emitters use (so all three
deliverables agree):

* component -> ECU with one invisible connector; cavities are the recovered
  pin designators; ``" imagedsp"`` reserved attribute carries the DIN symbol
  (exactly how the vendor's own Tapp3 example sets symbols),
* traced net -> wire, typed from the graph's net classification
  (power/ground/logical/bus/hv),
* duplicate memberships of one component on one net -> ``ARC`` wires joined
  to the two cavities (component-internal connections),
* printed net names -> ``SIGNAL`` modules grouping their wires.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

from docling_serve.schematic.eevision import (
    cavity_ids_for,
    din_symbol,
    net_connection_plan,
    net_label,
    safe_id,
    wire_type,
)

#: Environment variable naming the directory that contains ``PedbCreator.py``.
PEDB_DIR_ENV = "EEVISION_PEDB_DIR"


class EdbCreatorUnavailable(RuntimeError):
    """Raised when the vendor's EDB Creator wrapper cannot be imported."""


def load_pedb_creator() -> Any:
    """Import the vendor's ``PedbCreator`` module, honoring ``EEVISION_PEDB_DIR``."""
    pedb_dir = (os.environ.get(PEDB_DIR_ENV) or "").strip()
    if pedb_dir and pedb_dir not in sys.path:
        sys.path.insert(0, pedb_dir)
    try:
        return importlib.import_module("PedbCreator")
    except ImportError as error:
        raise EdbCreatorUnavailable(
            "PedbCreator is not importable. Install the EEvision EDB Creator "
            f"API and set {PEDB_DIR_ENV} to the directory containing PedbCreator.py."
        ) from error


def _new_wire(edb: Any, pedb: Any, name: str, classified: str) -> Any:
    """Create a wire, applying the EdbWireType when the wrapper supports it."""
    wire_types = getattr(pedb, "EdbWireType", None)
    type_value = None
    if wire_types is not None and classified:
        type_value = getattr(wire_types, classified.upper(), None)
    if type_value is not None:
        try:
            return edb.NewWire(name, type_value)
        except TypeError:
            pass  # older wrapper without the type argument
    return edb.NewWire(name)


def graph_to_edb(  # noqa: C901
    graph: dict[str, Any], output_path: Path, *, source_name: str
) -> dict[str, Any]:
    """Write ``output_path`` as a native EEvision EDB; returns build stats.

    Raises :class:`EdbCreatorUnavailable` when the vendor wrapper is missing —
    callers treat ``.edb`` as an optional artifact and record the reason.
    """
    pedb = load_pedb_creator()
    component_types = pedb.EdbComponentType
    connector_types = pedb.EdbConnectorType
    module_types = pedb.EdbModuleType

    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]
    raw_ids = {str(c.get("id")) for c in components}
    cavities = {raw_id: cavity_ids_for(raw_id, nets) for raw_id in raw_ids}
    plan = net_connection_plan(nets, raw_ids, cavities)

    edb = pedb.Edb()
    title = (graph.get("pages") or [{}])[0].get("titleBlock") or {}
    edb.NewAttr(None, "Source", source_name)
    if title.get("title"):
        edb.NewAttr(None, "Title", str(title["title"]))

    # Components: ECU + one invisible connector + pin-designator cavities.
    cavity_handles: dict[tuple[str, str], Any] = {}
    for order, component in enumerate(components, start=1):
        raw_id = str(component.get("id"))
        comp_id = safe_id(raw_id, f"C{order:04d}")
        display = str(component.get("refDes") or comp_id)
        comp = edb.NewComponent(display, component_types.ECU)
        symbol = din_symbol(component)
        if symbol:
            edb.NewAttr(comp, " imagedsp", f"{symbol},40,40")
        for field, label in (
            ("partNumber", "Part No"),
            ("type", "Component Type"),
            ("value", "Value"),
            ("location", "Location"),
        ):
            if component.get(field):
                edb.NewAttr(comp, label, str(component[field]))
        connector = edb.NewConnector(comp, "A", connector_types.INVISIBLE)
        for membership in sorted(cavities.get(raw_id, {})):
            cavity_id = cavities[raw_id][membership]
            if (raw_id, cavity_id) in cavity_handles:
                continue
            cavity_handles[(raw_id, cavity_id)] = edb.NewCavity(connector, cavity_id)

    # Wires + joins + component-internal ARC wires.
    stats = {"components": len(components), "wires": 0, "arcs": 0, "signals": 0}
    wires_by_signal: dict[str, list[Any]] = {}
    arc_counter = 0
    for index, (net, entry) in enumerate(zip(nets, plan), start=1):
        wire_ref = safe_id(net.get("wireId") or net.get("id"), f"W{index:03d}")
        label = net_label(net, index)
        wire = _new_wire(edb, pedb, label if label else wire_ref, wire_type(net))
        stats["wires"] += 1
        for comp_id, cavity_id in entry["endpoints"]:
            handle = cavity_handles.get((comp_id, cavity_id))
            if handle is not None:
                edb.Join(handle, wire)
        for comp_id, first_cavity, extra_cavity in entry["arcs"]:
            first = cavity_handles.get((comp_id, first_cavity))
            extra = cavity_handles.get((comp_id, extra_cavity))
            if first is None or extra is None:
                continue
            arc_counter += 1
            arc = _new_wire(edb, pedb, f"ARC{arc_counter:03d}", "arc")
            edb.Join(first, arc)
            edb.Join(extra, arc)
            stats["arcs"] += 1
        printed = str(net.get("name") or "").strip()
        if printed and printed != str(net.get("wireId") or ""):
            wires_by_signal.setdefault(printed, []).append(wire)

    # Signal modules from printed net names.
    for name, wires in sorted(wires_by_signal.items()):
        module = edb.NewModule(name, module_types.SIGNAL)
        for wire in wires:
            edb.AddObject2Module(module, wire)
        stats["signals"] += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    edb.SaveFile(str(output_path))
    return stats
