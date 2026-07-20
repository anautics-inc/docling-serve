"""EEvision delivery emitters: wire typing, ARC handling, signals, EDB build.

Pure-function tests over a small ``captify.schematic.v1`` fixture graph that
exercises every mapping rule: net classification (graph class first, printed
name fallback), component-internal connections (duplicate memberships ->
ARC), printed-name signal modules, no-wire components, and the native-EDB
builder driven against a fake ``PedbCreator`` recording the vendor API calls.
"""

import csv
import io
import json
import sys
from enum import Enum
from pathlib import Path

import pytest

from docling_serve.schematic.edml import graph_to_edml
from docling_serve.schematic.eevision import (
    cavity_ids_for,
    graph_to_eevision_csv,
    net_connection_plan,
    wire_type,
)


def fixture_graph() -> dict:
    """Two-resistor + relay graph covering every emitter rule."""
    return {
        "schema": "captify.schematic.v1",
        "pages": [{"number": 1, "titleBlock": {"title": "Test Fixture"}}],
        "components": [
            {"id": "C1", "refDes": "R1", "type": "Resistor", "value": "1k"},
            {"id": "C2", "refDes": "K1", "type": "Relay", "partNumber": "REL-9"},
            {"id": "C3", "refDes": "R9", "type": "Resistor"},  # unconnected
        ],
        "nets": [
            {
                # Graph-classified power net named HV (name regex can't tell).
                "id": "n1",
                "name": "HV",
                "wireId": "W001",
                "class": "power",
                "signalType": "power",
                "nodes": [
                    {"component": "C1", "pin": "1"},
                    {"component": "C2", "pin": "A1"},
                ],
            },
            {
                # Unclassified net where K1 appears TWICE -> one endpoint + one arc.
                "id": "n2",
                "name": None,
                "wireId": "W002",
                "nodes": [
                    {"component": "C1", "pin": "2"},
                    {"component": "C2", "pin": "A2"},
                    {"component": "C2", "pin": "B2"},
                ],
            },
            {
                # Printed net name -> signal module; classified by name fallback.
                "id": "n3",
                "name": "CAN_BUS",
                "wireId": "W003",
                "nodes": [{"component": "C2", "pin": "B1"}],
            },
        ],
    }


# --------------------------------------------------------------------------- #
# wire_type                                                                    #
# --------------------------------------------------------------------------- #


def test_wire_type_prefers_graph_classification() -> None:
    assert wire_type({"name": "HV", "class": "power"}) == "power"
    assert wire_type({"name": "anything", "signalType": "ground"}) == "ground"
    assert wire_type({"name": "X", "class": "signal"}) == "logical"


def test_wire_type_name_fallbacks() -> None:
    assert wire_type({"name": "GND"}) == "ground"
    assert wire_type({"name": "P315N"}) == "ground"  # MIL-W-5088 ground
    assert wire_type({"name": "HV"}) == "hv"
    assert wire_type({"name": "CAN High"}) == "bus"
    assert wire_type({"name": "+12VDC"}) == "power"
    assert wire_type({"name": "CATHODE_0"}) == ""


# --------------------------------------------------------------------------- #
# net_connection_plan                                                          #
# --------------------------------------------------------------------------- #


def test_plan_splits_duplicate_membership_into_arc() -> None:
    graph = fixture_graph()
    nets = graph["nets"]
    ids = {c["id"] for c in graph["components"]}
    cavities = {cid: cavity_ids_for(cid, nets) for cid in ids}
    plan = net_connection_plan(nets, ids, cavities)

    assert plan[0]["endpoints"] == [("C1", "1"), ("C2", "A1")]
    assert plan[0]["arcs"] == []
    # K1's second membership on n2 becomes an arc A2 -> B2, not an endpoint.
    assert plan[1]["endpoints"] == [("C1", "2"), ("C2", "A2")]
    assert plan[1]["arcs"] == [("C2", "A2", "B2")]


# --------------------------------------------------------------------------- #
# excel2edb CSV                                                                #
# --------------------------------------------------------------------------- #


def csv_rows(graph: dict) -> list[dict]:
    text = graph_to_eevision_csv(graph, source_name="fixture.pdf")
    return list(csv.DictReader(io.StringIO(text)))


def test_csv_has_no_undocumented_imagedsp_column() -> None:
    text = graph_to_eevision_csv(fixture_graph(), source_name="fixture.pdf")
    header = text.splitlines()[0]
    assert "imagedsp" not in header.lower()


def test_csv_types_wires_from_graph_class() -> None:
    rows = csv_rows(fixture_graph())
    hv = next(r for r in rows if r["Name"] == "HV")
    assert hv["Type"] == "POWER"
    can = next(r for r in rows if r["Name"] == "CAN_BUS")
    assert can["Type"] == "BUS"


def test_csv_emits_arc_rows_for_internal_connections() -> None:
    rows = csv_rows(fixture_graph())
    arcs = [r for r in rows if r["Type"] == "ARC"]
    assert len(arcs) == 1
    arc = arcs[0]
    assert arc["A-Comp"] == arc["B-Comp"] == "C2"
    assert {arc["A-Cav"], arc["B-Cav"]} == {"A2", "B2"}
    # The wire rows themselves never pair a component with itself any more.
    wire_rows = [r for r in rows if r["Wire"] and r["Type"] != "ARC"]
    assert all(not (r["A-Comp"] and r["A-Comp"] == r["B-Comp"]) for r in wire_rows)


def test_csv_keeps_no_wire_rows_for_unconnected_components() -> None:
    rows = csv_rows(fixture_graph())
    nowire = [r for r in rows if not r["Wire"]]
    assert len(nowire) == 1
    assert nowire[0]["A-Comp"] == "C3"


# --------------------------------------------------------------------------- #
# EDML                                                                         #
# --------------------------------------------------------------------------- #


def test_edml_emits_arcs_signals_and_types() -> None:
    edml = graph_to_edml(fixture_graph(), source_name="fixture.pdf")

    # Graph-classified power wire.
    assert 'Wire W001 | Name = "HV", Type = power' in edml
    # Internal connection is an Arc, and the extra membership is NOT joined.
    assert "Arc C2_arc1 (A.A2, A.B2);" in edml
    join_lines = [line for line in edml.splitlines() if line.startswith("Join")]
    assert not any("A.B2 ->" in line for line in join_lines)
    # Printed net names become Signal modules; assigned ids do not.
    assert 'Signal CAN_BUS (W003) | Name = "CAN_BUS";' in edml
    assert 'Signal HV (W001) | Name = "HV";' in edml
    assert "Signal W002" not in edml
    # Declaration order: every Wire statement precedes the first Component.
    first_component = edml.index("\nComponent ")
    last_wire = edml.rindex("\nWire ")
    assert last_wire < first_component


# --------------------------------------------------------------------------- #
# Native EDB build (fake vendor wrapper)                                       #
# --------------------------------------------------------------------------- #


class _FakeEnum(Enum):
    def __str__(self) -> str:  # pragma: no cover - debug help
        return self.name


class FakeComponentType(_FakeEnum):
    UNDEF = 0
    ECU = 1


class FakeConnectorType(_FakeEnum):
    UNDEF = 0
    INVISIBLE = 3


class FakeCavityType(_FakeEnum):
    UNDEF = 0


class FakeWireType(_FakeEnum):
    UNDEF = 0
    GROUND = 1
    POWER = 2
    LOGICAL = 3
    BUS = 4
    HV = 5
    ARC = 6


class FakeModuleType(_FakeEnum):
    UNDEF = 0
    SIGNAL = 3


class FakeEdb:
    """Records the EDB Creator call sequence and writes a JSON 'edb'."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._handles = 0

    def _handle(self, kind: str) -> str:
        self._handles += 1
        return f"{kind}#{self._handles}"

    def NewComponent(self, name, ctype):
        handle = self._handle("comp")
        self.calls.append(("NewComponent", handle, name, ctype.name))
        return handle

    def NewConnector(self, comp, name, ctype=FakeConnectorType.UNDEF):
        handle = self._handle("conn")
        self.calls.append(("NewConnector", handle, comp, name, ctype.name))
        return handle

    def NewCavity(self, conn, name, ctype=FakeCavityType.UNDEF):
        handle = self._handle("cav")
        self.calls.append(("NewCavity", handle, conn, name, ctype.name))
        return handle

    def NewWire(self, name, wtype=FakeWireType.UNDEF):
        handle = self._handle("wire")
        self.calls.append(("NewWire", handle, name, wtype.name))
        return handle

    def NewModule(self, name, mtype=FakeModuleType.UNDEF):
        handle = self._handle("mod")
        self.calls.append(("NewModule", handle, name, mtype.name))
        return handle

    def NewAttr(self, obj, name, value):
        self.calls.append(("NewAttr", obj, name, value))
        return True

    def Join(self, cavity, wire):
        self.calls.append(("Join", cavity, wire))
        return True

    def AddObject2Module(self, module, obj):
        self.calls.append(("AddObject2Module", module, obj))
        return True

    def SaveFile(self, filename):
        Path(filename).write_text(json.dumps({"calls": self.calls}, default=str))
        self.calls.append(("SaveFile", filename))
        return True


@pytest.fixture()
def fake_pedb(monkeypatch):
    import types

    module = types.ModuleType("PedbCreator")
    module.Edb = FakeEdb
    module.EdbComponentType = FakeComponentType
    module.EdbConnectorType = FakeConnectorType
    module.EdbCavityType = FakeCavityType
    module.EdbWireType = FakeWireType
    module.EdbModuleType = FakeModuleType
    monkeypatch.setitem(sys.modules, "PedbCreator", module)
    return module


def test_graph_to_edb_builds_and_saves(tmp_path, fake_pedb) -> None:
    from docling_serve.schematic.edb import graph_to_edb

    out = tmp_path / "fixture.edb"
    stats = graph_to_edb(fixture_graph(), out, source_name="fixture.pdf")

    assert out.is_file(), "SaveFile must produce the .edb"
    assert stats == {"components": 3, "wires": 3, "arcs": 1, "signals": 2}

    recorded = json.loads(out.read_text())["calls"]
    by_kind = {}
    for call in recorded:
        by_kind.setdefault(call[0], []).append(call)

    # All three components exist as ECUs with an invisible connector each.
    assert len(by_kind["NewComponent"]) == 3
    assert all(c[3] == "ECU" for c in by_kind["NewComponent"])
    assert len(by_kind["NewConnector"]) == 3
    assert all(c[4] == "INVISIBLE" for c in by_kind["NewConnector"])

    # Wires: HV typed POWER from the graph class, CAN_BUS as BUS, plus one ARC.
    wires = {c[2]: c[3] for c in by_kind["NewWire"]}
    assert wires["HV"] == "POWER"
    assert wires["CAN_BUS"] == "BUS"
    assert wires["ARC001"] == "ARC"

    # The arc joins two cavities; with its wire that is 2 joins beyond the
    # 4 wire-endpoint joins (HV: 2, W002: 2, CAN_BUS: 1) = 7 total.
    assert len(by_kind["Join"]) == 7

    # DIN symbols ride the vendor-documented reserved attribute.
    imagedsp = [c for c in recorded if c[0] == "NewAttr" and c[2] == " imagedsp"]
    assert len(imagedsp) == 3  # R1 + R9 resistors -> R; K1 relay -> K
    assert {c[3] for c in imagedsp} == {"R,40,40", "K,40,40"}

    # Signal modules for the two printed names, each containing its wire.
    modules = {c[2]: c[1] for c in by_kind["NewModule"]}
    assert set(modules) == {"HV", "CAN_BUS"}
    assert all(c[3] == "SIGNAL" for c in by_kind["NewModule"])
    assert len(by_kind["AddObject2Module"]) == 2


def test_graph_to_edb_reports_missing_vendor_library(tmp_path, monkeypatch) -> None:
    from docling_serve.schematic.edb import EdbCreatorUnavailable, graph_to_edb

    monkeypatch.setitem(sys.modules, "PedbCreator", None)
    with pytest.raises((EdbCreatorUnavailable, ImportError)):
        graph_to_edb(fixture_graph(), tmp_path / "x.edb", source_name="x.pdf")
