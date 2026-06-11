"""Unit tests for the connector / extractor / enhancer pipeline.

These cover the dispatch and pure-logic seams without hitting Bedrock, mdbtools,
or AWS: the registry selection rules, the schematic graph normalisation +
KiCad netlist serialisation (model output mocked), the Access extractor (mdbtools
mocked), the image-context enhancer (Bedrock mocked), and connector resolution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docling_serve.connectors import (
    ConnectorError,
    FileConnector,
    available_connectors,
    available_services,
    resolve_connector,
)
from docling_serve.deep_document.schema_validation import validate_artifact
from docling_serve.extractors import (
    ExtractionContext,
    access_extractor as access_mod,
    select_extractor,
)
from docling_serve.extractors.kicad_sch import (
    KicadConversionError,
    parse_path_data,
    svg_to_kicad_sch,
)
from docling_serve.extractors.net_trace import ComponentBox, TracedNet, trace_nets
from docling_serve.extractors.netlist import graph_to_kicad_netlist
from docling_serve.extractors.schematic_extractor import (
    _cached_understand_json,
    _merge_detected_components,
    _normalize_graph,
    _scale_bboxes_to_pt,
)

FIXTURES = Path(__file__).parent / "test_files"


def _ctx(tmp_path: Path, name: str, *, profile: str = "default", **kw) -> ExtractionContext:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    return ExtractionContext(
        source_path=tmp_path / name,
        bundle_dir=bundle,
        media_dir=bundle / "media",
        source_manifest_key=f"task:test:{Path(name).stem}",
        task_id="test",
        profile=profile,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Extractor registry selection                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "profile", "expected"),
    [
        ("db.accdb", "default", "extract_access"),
        ("db.mdb", "default", "extract_access"),
        ("data.bin", "access", "extract_access"),
        ("drawing.pdf", "schematic", "extract_schematic"),
        ("drawing.png", "drawing", "extract_schematic"),
        ("deck.pptx", "default", "extract_ppt"),
        ("report.pdf", "default", "extract_doc"),
        ("notes.docx", "default", "extract_doc"),
    ],
)
def test_select_extractor(tmp_path, name, profile, expected):
    assert select_extractor(_ctx(tmp_path, name, profile=profile)).name == expected


def test_schematic_not_selected_for_plain_pdf(tmp_path):
    # A schematic-eligible suffix must still default to the generic extractor
    # unless the profile asks for it (or auto-detection fires).
    assert select_extractor(_ctx(tmp_path, "manual.pdf")).name == "extract_doc"


# --------------------------------------------------------------------------- #
# Schematic graph normalisation + KiCad netlist                               #
# --------------------------------------------------------------------------- #


def _model_page() -> dict:
    return {
        "__page__": 1,
        "titleBlock": {"title": "Power Supply", "drawingNumber": "SCH-001"},
        "components": [
            {"refDes": "R1", "type": "resistor", "value": "10k", "pins": [{"number": "1"}, {"number": "2"}]},
            {"refDes": "C1", "type": "capacitor", "value": "100n", "pins": [{"number": "1"}]},
        ],
        "nets": [
            {"name": "GND", "nodes": [{"refDes": "R1", "pin": "2"}, {"refDes": "C1", "pin": "1"}]},
            {"name": "VCC", "nodes": [{"refDes": "R1", "pin": "1"}]},
        ],
        "confidence": 0.9,
        "warnings": [],
    }


def test_normalize_graph_assigns_ids_and_resolves_nets():
    graph = _normalize_graph(
        [_model_page()],
        source_name="psu.pdf",
        model_id="test-model",
        understood=True,
        svg_paths=[],
        page_images=[],
        warnings=[],
    )
    assert graph["artifactKind"] == "captify.schematic.v1"
    assert graph["model"]["understood"] is True
    assert [c["refDes"] for c in graph["components"]] == ["R1", "C1"]
    # Component ids are stable and net nodes resolve to them.
    ids = {c["refDes"]: c["id"] for c in graph["components"]}
    gnd = next(n for n in graph["nets"] if n["name"] == "GND")
    assert {node["component"] for node in gnd["nodes"]} == {ids["R1"], ids["C1"]}
    # Validates against the published schema.
    validate_artifact(graph, "schematic-graph.schema.json")


def test_graph_to_kicad_netlist_roundtrip():
    graph = _normalize_graph(
        [_model_page()],
        source_name="psu.pdf",
        model_id="test-model",
        understood=True,
        svg_paths=[],
        page_images=[],
        warnings=[],
    )
    netlist = graph_to_kicad_netlist(graph, source_name="psu.pdf")
    assert netlist.startswith("(export")
    assert '(comp (ref "R1")' in netlist
    assert '(net (code "1") (name "GND")' in netlist
    assert '(node (ref "R1") (pin "2"))' in netlist
    assert '(node (ref "C1") (pin "1"))' in netlist


def test_normalize_graph_geometry_only_when_no_model_output():
    graph = _normalize_graph(
        [],
        source_name="psu.pdf",
        model_id="test-model",
        understood=False,
        svg_paths=["schematic/schematic.svg"],
        page_images=[],
        warnings=["bedrock_disabled"],
    )
    assert graph["components"] == []
    assert graph["nets"] == []
    assert graph["model"]["understood"] is False
    assert any("model understanding unavailable" in n for n in graph["notes"])
    validate_artifact(graph, "schematic-graph.schema.json")


# --------------------------------------------------------------------------- #
# Geometric net tracing                                                        #
# --------------------------------------------------------------------------- #


def test_trace_nets_connects_through_t_junction():
    # R1 ---+--- R2 with a tap down to R3: one net of three components.
    boxes = [
        ComponentBox("R1", 0, 0, 10, 10),
        ComponentBox("R2", 90, 0, 100, 10),
        ComponentBox("R3", 45, 90, 55, 100),
    ]
    wires = [
        [(10.0, 5.0), (90.0, 5.0)],  # horizontal bus R1->R2
        [(50.0, 5.0), (50.0, 90.0)],  # T-tap down to R3
    ]
    nets = trace_nets(wires, boxes)
    assert len(nets) == 1
    assert nets[0].components == ["R1", "R2", "R3"]
    # Physical connection points sit where the wires meet the boxes.
    assert nets[0].attachments["R1"] == [(10.0, 5.0)]
    assert nets[0].attachments["R2"] == [(90.0, 5.0)]
    assert nets[0].attachments["R3"] == [(50.0, 90.0)]


def test_trace_nets_two_attachments_for_double_connection():
    # U1 joins the same net on two separate pins (two wires into one box).
    boxes = [ComponentBox("U1", 0, 0, 20, 40), ComponentBox("R1", 80, 0, 100, 40)]
    wires = [
        [(20.0, 10.0), (80.0, 10.0)],
        [(20.0, 30.0), (60.0, 30.0), (60.0, 10.0)],  # second pin joins the bus
    ]
    nets = trace_nets(wires, boxes)
    assert len(nets) == 1
    assert sorted(nets[0].attachments["U1"]) == [(20.0, 10.0), (20.0, 30.0)]


def test_trace_nets_x_crossing_does_not_connect():
    # Two wires crossing mid-span without a junction stay separate nets.
    boxes = [
        ComponentBox("A", 0, 40, 10, 60),
        ComponentBox("B", 190, 40, 200, 60),
        ComponentBox("C", 90, 0, 110, 10),
        ComponentBox("D", 90, 190, 110, 200),
    ]
    wires = [
        [(10.0, 50.0), (190.0, 50.0)],  # A -- B horizontal
        [(100.0, 10.0), (100.0, 190.0)],  # C -- D vertical, crosses mid-span
    ]
    nets = trace_nets(wires, boxes)
    assert sorted(net.components for net in nets) == [["A", "B"], ["C", "D"]]


def test_trace_nets_cuts_symbol_artwork_inside_boxes():
    # A wire passing straight through a component box splits into two nets,
    # both attached to that component (its two pins).
    boxes = [
        ComponentBox("R1", 40, 0, 60, 10),
        ComponentBox("J1", 0, 0, 5, 10),
        ComponentBox("J2", 95, 0, 100, 10),
    ]
    wires = [
        [(5.0, 5.0), (95.0, 5.0)],  # runs through R1's box
        [(42.0, 2.0), (58.0, 8.0)],  # symbol artwork fully inside R1: dropped
    ]
    nets = trace_nets(wires, boxes)
    assert sorted(net.components for net in nets) == [["J1", "R1"], ["J2", "R1"]]


def test_normalize_graph_carries_part_identity_and_grounds():
    page = _model_page()
    page["components"][0].update(
        {
            "partNumber": "KIDDE 870929",
            "location": "RH SIDE, STA 21",
            "confidence": 0.92,
            "pins": [{"number": "1", "status": "connected"}, {"number": "2", "status": "nc"}],
        }
    )
    page["nets"][0].update({"wireId": "A8B22", "gauge": "22", "signalType": "ground"})
    page["groundPoints"] = [{"id": None, "name": "E1", "location": "STA 21 STUD"}]
    graph = _normalize_graph(
        [page],
        source_name="harness.pdf",
        model_id="test-model",
        understood=True,
        svg_paths=[],
        page_images=[],
        warnings=[],
    )
    component = graph["components"][0]
    assert component["partNumber"] == "KIDDE 870929"
    assert component["location"] == "RH SIDE, STA 21"
    assert component["confidence"] == 0.92
    assert [pin["status"] for pin in component["pins"]] == ["connected", "nc"]
    gnd = next(net for net in graph["nets"] if net["name"] == "GND")
    assert (gnd["wireId"], gnd["gauge"], gnd["signalType"]) == ("A8B22", "22", "ground")
    assert graph["groundPoints"] == [
        {"id": "GND001-01", "name": "E1", "location": "STA 21 STUD", "page": 1}
    ]
    validate_artifact(graph, "schematic-graph.schema.json")


def test_traced_nets_adopt_wire_metadata_from_model_nets():
    page = _model_page()
    page["nets"][0].update({"wireId": "A8B22", "gauge": "22", "signalType": "ground"})
    graph = _normalize_graph(
        [page],
        source_name="harness.pdf",
        model_id="test-model",
        understood=True,
        svg_paths=[],
        page_images=[],
        warnings=[],
        traced_nets_by_page={1: [TracedNet(components=["R1", "C1"])]},
    )
    net = graph["nets"][0]
    assert net["source"] == "geometry"
    assert (net["name"], net["wireId"], net["gauge"], net["signalType"]) == (
        "GND",
        "A8B22",
        "22",
        "ground",
    )
    # Pin numbers adopted from the matched model net (geometry can't see pins).
    pins = {node["component"]: node["pin"] for node in net["nodes"]}
    ids = {c["refDes"]: c["id"] for c in graph["components"]}
    assert pins[ids["R1"]] == "2"
    assert pins[ids["C1"]] == "1"
    assert all(
        node.get("pinSource") == "model" for node in net["nodes"] if node["pin"] is not None
    )


def test_normalize_graph_prefers_traced_nets_and_recovers_names():
    page = _model_page()
    traced = {
        1: [
            TracedNet(components=["R1", "C1"]),
            TracedNet(components=["R1", "U9"]),  # U9 unknown: kept as raw ref
        ]
    }
    graph = _normalize_graph(
        [page],
        source_name="psu.pdf",
        model_id="test-model",
        understood=True,
        svg_paths=["schematic/schematic.svg"],
        page_images=[],
        warnings=[],
        traced_nets_by_page=traced,
    )
    assert all(net.get("source") == "geometry" for net in graph["nets"])
    ids = {c["refDes"]: c["id"] for c in graph["components"]}
    named = next(n for n in graph["nets"] if n["name"] == "GND")
    assert {node["component"] for node in named["nodes"]} == {ids["R1"], ids["C1"]}
    unnamed = next(n for n in graph["nets"] if n["name"] is None)
    assert {node["component"] for node in unnamed["nodes"]} == {ids["R1"], "U9"}
    validate_artifact(graph, "schematic-graph.schema.json")


# --------------------------------------------------------------------------- #
# EDML export                                                                  #
# --------------------------------------------------------------------------- #


def test_graph_to_edml_components_grounds_wires():
    from docling_serve.extractors.edml import graph_to_edml

    graph = {
        "components": [
            {
                "id": "C0001",
                "refDes": "V1",
                "type": "valve",
                "value": "GUN CHARGING VALVE",
                "partNumber": "KIDDE 870929",
                "location": "RH SIDE STA 21",
                "pins": [{"name": "PIN A"}, {"name": "PIN B"}],
            },
            {"id": "C0002", "refDes": "E1", "type": "ground stud"},
        ],
        "nets": [
            {
                "id": "N1",
                "name": "A8B22",
                "gauge": "22 AWG",
                "signalType": "control",
                "nodes": [
                    {"component": "C0001", "pin": "PIN A"},
                    {"component": "C0002", "pin": None},
                ],
            }
        ],
    }
    edml = graph_to_edml(graph, source_name="fixture.pdf")
    assert 'Component V1 | Name="GUN CHARGING VALVE", "Location" = "RH SIDE STA 21", "PartNumber" = "KIDDE 870929"' in edml
    assert 'Cavity 1 | Name="PIN A";' in edml
    assert "Join A.PIN A -> W1;" in edml
    assert "Eyelet E1" in edml
    assert "Join 1 -> W1;" in edml
    assert 'Wire W1 | Name="A8B22", "Gauge" = "22 AWG", "SignalType" = "control";' in edml
    assert edml.endswith("// End of EDML\n")


# --------------------------------------------------------------------------- #
# Wire-label adoption (scanned drawings' OCR text layer)                       #
# --------------------------------------------------------------------------- #


def test_wire_id_candidates_normalizes_ocr_confusions():
    from docling_serve.extractors.schematic_extractor import _wire_id_candidates

    assert _wire_id_candidates("A68A2ON") == ["A68A20N"]  # O -> 0
    assert _wire_id_candidates("AI9CI8") == ["A19C18"]  # I -> 1
    assert _wire_id_candidates("A -A8C22") == ["A8C22"]  # junk prefix
    assert _wire_id_candidates("A8A22 -0") == ["A8A22"]  # junk suffix
    assert _wire_id_candidates("A33") == ["A33"]  # short station wire
    assert _wire_id_candidates("VALVE") == []
    assert _wire_id_candidates("R H SIDE") == []
    assert _wire_id_candidates("MONITORED") == []  # word, not an id, despite O/I subs
    assert _wire_id_candidates("") == []


def test_adopt_wire_labels_names_nearest_segment():
    from docling_serve.extractors.schematic_extractor import _adopt_wire_labels

    nets = [
        {"name": None, "segments": [[100.0, 300.0, 400.0, 300.0]]},
        {"name": None, "segments": [[100.0, 500.0, 400.0, 500.0]]},
        {"name": "KEEP", "segments": [[0.0, 0.0, 10.0, 0.0]]},
    ]
    labels = [
        (200.0, 292.0, 240.0, 299.0, "A8B22"),  # sits just above net 0
        (200.0, 495.0, 250.0, 505.0, "AI9CI8"),  # OCR-mangled, on net 1
        (700.0, 700.0, 720.0, 710.0, "A7A22"),  # far from everything
    ]
    assert _adopt_wire_labels(nets, labels) == 2
    assert nets[0]["name"] == "A8B22"
    assert nets[1]["name"] == "A19C18"
    assert nets[2]["name"] == "KEEP"


# --------------------------------------------------------------------------- #
# Raster wire extraction (scanned drawings)                                    #
# --------------------------------------------------------------------------- #


def _synthetic_scan_png() -> bytes:
    """A 800x600 'scan': two thin wires, a thick frame, a red annotation box,
    and a text label sitting on the horizontal wire."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 600), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 790, 590), outline=(0, 0, 0), width=8)  # page frame
    draw.line((100, 300, 450, 300), fill=(0, 0, 0), width=2)  # H wire
    draw.line((400, 150, 400, 450), fill=(0, 0, 0), width=2)  # V wire (< border span)
    draw.rectangle((150, 150, 350, 250), outline=(220, 30, 30), width=4)  # annotation
    draw.text((250, 290), "A8B22", fill=(0, 0, 0))  # label ON the wire
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_raster_wire_polylines_extracts_wires_drops_frame_and_annotation():
    from docling_serve.extractors.raster_lines import raster_wire_polylines

    # Page pt == px for a simple 1:1 mapping.
    polylines = raster_wire_polylines(
        _synthetic_scan_png(),
        page_size_pt=(800, 600),
        text_boxes_pt=[(245, 285, 310, 305)],
    )
    horizontals = [p for p in polylines if abs(p[0][1] - p[1][1]) < 3 and abs(p[0][1] - 300) < 6]
    verticals = [p for p in polylines if abs(p[0][0] - p[1][0]) < 3 and abs(p[0][0] - 400) < 6]
    assert horizontals, "horizontal wire not extracted"
    assert verticals, "vertical wire not extracted"
    # The thick page frame and the red annotation box must NOT come through.
    for poly in polylines:
        (x0, y0), (x1, y1) = poly
        assert max(abs(x1 - x0), abs(y1 - y0)) < 0.65 * 800
        assert not (140 < x0 < 360 and 140 < y0 < 260 and abs(y1 - y0) < 5 and abs(y0 - 150) < 8)


def test_raster_wire_polylines_handles_garbage_input():
    from docling_serve.extractors.raster_lines import raster_wire_polylines

    assert raster_wire_polylines(b"not a png", page_size_pt=(100, 100)) == []
    assert raster_wire_polylines(_synthetic_scan_png(), page_size_pt=(0, 0)) == []


# --------------------------------------------------------------------------- #
# Scanned-drawing fallbacks (raster KiCad backdrop, model-net adequacy)        #
# --------------------------------------------------------------------------- #


def test_raster_page_to_kicad_sch_embeds_standard_image_token():
    import io

    from PIL import Image

    from docling_serve.extractors.kicad_sch import raster_page_to_kicad_sch

    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), (255, 255, 255)).save(buffer, format="PNG")
    text = raster_page_to_kicad_sch(
        buffer.getvalue(), dpi=200, width_px=400, height_px=300, title="scan"
    )
    assert text.startswith("(kicad_sch")
    assert "(image (at" in text
    assert "(scale 1.5)" in text  # 300 base dpi / 200 render dpi
    assert '(data\n      "' in text
    assert '(sheet_instances (path "/" (page "1")))' in text


def test_tracing_adequacy_rejects_frame_only_traces():
    from docling_serve.extractors.net_trace import TracedNet
    from docling_serve.extractors.schematic_extractor import _tracing_adequate

    model_nets = [
        {"nodes": [{"refDes": f"C{i}"}, {"refDes": f"C{i + 1}"}]} for i in range(10)
    ]
    # A scan: two junk traces from the page frame vs 20 model memberships.
    junk = [TracedNet(components=["C1", "C2"], segments=[], attachments={})]
    assert _tracing_adequate(junk, model_nets) is False
    # A vector drawing: traces cover most of what the model saw.
    rich = [
        TracedNet(components=[f"C{i}", f"C{i + 1}"], segments=[], attachments={})
        for i in range(8)
    ]
    assert _tracing_adequate(rich, model_nets) is True
    assert _tracing_adequate(junk, []) is True  # nothing to fall back to


# --------------------------------------------------------------------------- #
# KBL export (VDA 4964 — the EE Vision interchange path)                       #
# --------------------------------------------------------------------------- #

_KBL_SAMPLE_GRAPH = {
    "titleBlock": {"title": "Gun Charging", "drawingNumber": "MSX-001", "revision": "A"},
    "components": [
        {
            "id": "C0001",
            "refDes": "V1",
            "type": "valve",
            "partNumber": "KIDDE 870929",
            "location": "RH SIDE STA 21",
            "pins": [{"number": "A", "name": "PIN A"}, {"number": "B", "name": "PIN B"}],
        },
        {"id": "C0002", "refDes": "K1", "type": "relay", "pins": [{"number": "1"}, {"number": "2"}]},
        {"id": "C0003", "refDes": "E1", "type": "ground stud"},
    ],
    "nets": [
        {
            "id": "N1",
            "name": "A8B22",
            "gauge": "22 AWG",
            "signalType": "control",
            "nodes": [{"component": "C0001", "pin": "B"}, {"component": "C0002", "pin": "1"}],
        },
        {
            "id": "N2",
            "name": "GND",
            "nodes": [{"component": "C0001", "pin": "A"}],  # single-ended: no Connection
        },
    ],
}


def test_graph_to_kbl_is_schema_valid():
    """The generated KBL must validate against the OFFICIAL prostep/VDA XSD —
    this is the conformance gate for the EE Vision import path (EE Vision
    converts KBL into its native EDB model)."""
    from lxml import etree

    from docling_serve.extractors.kbl import graph_to_kbl

    kbl_text = graph_to_kbl(_KBL_SAMPLE_GRAPH, source_name="fixture.pdf")
    schema = etree.XMLSchema(etree.parse(str(FIXTURES / "KBL24_SR1.xsd")))
    document = etree.fromstring(kbl_text.encode())
    assert schema.validate(document), [str(e) for e in schema.error_log[:5]]


def test_graph_to_kbl_maps_connectivity():
    from lxml import etree

    from docling_serve.extractors.kbl import graph_to_kbl

    kbl_text = graph_to_kbl(_KBL_SAMPLE_GRAPH, source_name="fixture.pdf")
    document = etree.fromstring(kbl_text.encode())
    harness = document.find("Harness")
    connectors = harness.findall("Connector_occurrence")
    assert [c.findtext("Id") for c in connectors] == ["V1", "K1", "E1"]
    connections = harness.findall("Connection")
    assert len(connections) == 1  # the single-ended GND exports no connection
    assert connections[0].findtext("Signal_name") == "A8B22"
    extremities = connections[0].findall("Extremities")
    assert len(extremities) == 2
    wire_occurrences = harness.findall("General_wire_occurrence")
    assert len(wire_occurrences) == 2  # every net keeps its wire occurrence
    assert connections[0].findtext("Wire") == wire_occurrences[0].get("id")


def test_net_wires_sexpr_match_eeschema_save_format():
    from docling_serve.extractors.kicad_sch import net_wires_sexpr

    nets = [
        {
            "page": 1,
            "segments": [[100.0, 200.0, 300.0, 200.0], [100.0, 200.0, 300.0, 200.0]],
        },
        {"page": 2, "segments": [[1.0, 1.0, 2.0, 2.0]]},  # other page: excluded
    ]
    wires = net_wires_sexpr(nets, page_no=1)
    assert len(wires) == 1  # duplicate span collapses; page 2 excluded
    # eeschema 10 wire shape: pts in mm, default stroke, uuid.
    assert wires[0].startswith("  (wire (pts (xy ")
    assert "(stroke (width 0) (type default))" in wires[0]
    assert '(uuid "' in wires[0]


def test_net_wires_sexpr_emits_real_kicad_wires():
    """Traced net segments must become electrical (wire …) objects — not
    graphics — so KiCad recognizes the drawing's lines as wires."""
    from docling_serve.extractors.kicad_sch import inject_items, net_wires_sexpr

    nets = [
        {"id": "N1", "page": 1, "segments": [[72.0, 72.0, 144.0, 72.0]]},
        {"id": "N2", "page": 2, "segments": [[0.0, 0.0, 10.0, 0.0]]},  # other page
        {"id": "N3", "segments": []},  # model net without geometry
    ]
    wires = net_wires_sexpr(nets, page_no=1)
    assert len(wires) == 1
    assert "(wire (pts (xy 25.4 25.4) (xy 50.8 25.4))" in wires[0]
    assert "(stroke (width 0) (type default))" in wires[0]

    doc = '(kicad_sch\n  (lib_symbols)\n  (sheet_instances (path "/" (page "1")))\n)\n'
    injected = inject_items(doc, wires)
    assert injected.count("(wire (pts") == 1
    assert injected.index("(wire") < injected.index("(sheet_instances")


def test_kicad_cli_roundtrip_opens_generated_schematic(tmp_path):
    """REAL tool validation: KiCad itself must parse and plot our .kicad_sch."""
    import shutil as _shutil
    import subprocess

    if not _shutil.which("kicad-cli"):
        pytest.skip("kicad-cli not installed")
    from docling_serve.extractors.kicad_sch import svg_to_kicad_sch

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100pt" height="100pt" '
        'viewBox="0 0 100 100"><path d="M10 10 L90 10 L90 90" '
        'stroke="#000" fill="none"/></svg>'
    )
    sch = tmp_path / "generated.kicad_sch"
    sch.write_text(svg_to_kicad_sch(svg, title="roundtrip"))
    result = subprocess.run(
        ["kicad-cli", "sch", "export", "svg", "--output", str(tmp_path), str(sch)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "generated.svg").exists()


# --------------------------------------------------------------------------- #
# SPICE export (simulation; KiCad/ngspice + Altair SpiceVision family)         #
# --------------------------------------------------------------------------- #


def test_graph_to_spice_maps_primitives_and_subckts():
    from docling_serve.extractors.spice import graph_to_spice

    graph = {
        "titleBlock": {"title": "Gun Charging"},
        "components": [
            {"id": "C1", "refDes": "R1", "type": "resistor", "value": "10k"},
            {
                "id": "C2",
                "refDes": "V1",
                "type": "valve",
                "partNumber": "KIDDE 870929",
                "pins": [{"number": "A"}, {"number": "B"}],
            },
            {"id": "C3", "refDes": "K1", "type": "relay", "partNumber": "LEACH 9089-73P"},
        ],
        "nets": [
            {
                "id": "N1",
                "name": "A8B22",
                "nodes": [{"component": "C1"}, {"component": "C2"}],
            },
            {"id": "N2", "name": "GND", "nodes": [{"component": "C2"}, {"component": "C3"}]},
        ],
    }
    spice = graph_to_spice(graph, source_name="navair.pdf")
    assert spice.startswith("* Gun Charging")
    assert "RR1 A8B22" in spice  # primitive with printed value
    assert "10k" in spice
    # No vendor model -> typed electromechanicals get INFERRED first-order
    # physics (valve -> solenoid coil, relay -> relay coil), clearly labelled.
    assert "XV1 A8B22 GND INF_KIDDE_870929_2P" in spice  # both memberships as nodes
    assert ".subckt INF_KIDDE_870929_2P p1 p2" in spice
    assert "* INFERRED: solenoid coil winding as series R+L" in spice
    assert ".subckt INF_LEACH_9089_73P_2P" in spice
    assert spice.rstrip().endswith(".end")


def test_graph_to_spice_parses_in_ngspice(tmp_path):
    """REAL simulator validation: ngspice must accept the netlist syntax."""
    import shutil as _shutil
    import subprocess

    if not _shutil.which("ngspice"):
        pytest.skip("ngspice not installed")
    from docling_serve.extractors.spice import graph_to_spice

    graph = {
        "components": [
            {"id": "C1", "refDes": "R1", "type": "resistor", "value": "10k"},
            {"id": "C2", "refDes": "V1", "type": "valve", "pins": [{"number": "A"}, {"number": "B"}]},
        ],
        "nets": [
            {"id": "N1", "name": "A8B22", "nodes": [{"component": "C1", "pin": None}, {"component": "C2", "pin": None}]},
        ],
    }
    netlist = graph_to_spice(graph, source_name="fixture.pdf")
    # Syntax gate: elaborate + list the circuit, no analysis required. A
    # parse failure (bad element, unknown subckt) surfaces as "Error:".
    checked = netlist.replace(
        ".end\n", ".control\nlisting e\nquit\n.endc\n.end\n"
    )
    circuit = tmp_path / "test.cir"
    circuit.write_text(checked)
    result = subprocess.run(
        ["ngspice", "-b", str(circuit)], capture_output=True, text=True, timeout=60
    )
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode == 0, combined[:400]
    assert "error:" not in combined.replace("no error", ""), combined[:400]
    assert "xv1" in combined  # the subckt instance survived elaboration


def test_spice_inference_tiers():
    """Type-driven first-order physics: the inferred tier of model resolution."""
    from docling_serve.extractors.spice_inference import infer_subckt_body

    # Switch -> closed contact.
    body = infer_subckt_body({"type": "toggle switch"}, 2)
    assert body is not None and body.lines == ("R1 p1 p2 10m",)
    # Lamp honours the printed value.
    body = infer_subckt_body({"type": "indicator lamp", "value": "47"}, 2)
    assert body is not None and body.lines == ("R1 p1 p2 47",)
    assert "printed value" in body.rationale
    # Diode requires a .model card.
    body = infer_subckt_body({"type": "diode"}, 2)
    assert body is not None and body.lines == ("D1 p1 p2 DGEN",)
    # 3-pin transistor maps positionally.
    body = infer_subckt_body({"type": "NPN transistor"}, 3)
    assert body is not None and "QGENNPN" in body.lines[0]
    # Pin-count mismatch -> no inference (mis-wiring would be silent).
    assert infer_subckt_body({"type": "relay"}, 6) is None
    assert infer_subckt_body({"type": "NPN transistor"}, 2) is None
    # Unknown internals stay un-inferred.
    assert infer_subckt_body({"type": "microcontroller"}, 22) is None
    assert infer_subckt_body({"type": "connector"}, 2) is None
    assert infer_subckt_body({}, 2) is None


def test_graph_to_spice_inferred_models_parse_in_ngspice(tmp_path):
    """Inferred bodies (coil, contact, diode, transistor) elaborate in ngspice."""
    import shutil as _shutil
    import subprocess

    if not _shutil.which("ngspice"):
        pytest.skip("ngspice not installed")
    from docling_serve.extractors.spice import graph_to_spice

    graph = {
        "components": [
            {"id": "C1", "refDes": "K1", "type": "relay"},
            {"id": "C2", "refDes": "S1", "type": "switch"},
            {"id": "C3", "refDes": "DS1", "type": "lamp", "value": "28"},
            {"id": "C4", "refDes": "CR1", "type": "diode"},
            {"id": "C5", "refDes": "Q1", "type": "NPN transistor"},
        ],
        "nets": [
            {"id": "N1", "name": "BUS", "nodes": [
                {"component": "C1"}, {"component": "C2"}, {"component": "C3"},
                {"component": "C4"}, {"component": "C5"},
            ]},
            {"id": "N2", "name": "GND", "nodes": [
                {"component": "C1"}, {"component": "C2"}, {"component": "C3"},
                {"component": "C4"}, {"component": "C5"},
            ]},
            {"id": "N3", "name": "BASE", "nodes": [{"component": "C5"}]},
        ],
    }
    netlist = graph_to_spice(graph, source_name="inferred.pdf")
    assert "* INFERRED" in netlist
    assert ".model DGEN D()" in netlist
    assert ".model QGENNPN NPN(BF=100)" in netlist
    checked = netlist.replace(".end\n", ".control\nlisting e\nquit\n.endc\n.end\n")
    circuit = tmp_path / "inferred.cir"
    circuit.write_text(checked)
    result = subprocess.run(
        ["ngspice", "-b", str(circuit)], capture_output=True, text=True, timeout=60
    )
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode == 0, combined[:400]
    assert "error:" not in combined.replace("no error", ""), combined[:400]
    assert "xk1" in combined and "xq1" in combined


# --------------------------------------------------------------------------- #
# Catalog-driven SPICE model binding                                           #
# --------------------------------------------------------------------------- #


def _model_library(tmp_path):
    """A synthetic vendor-model library keyed by normalized part number."""
    library = tmp_path / "spice-models"
    library.mkdir()
    (library / "KIDDE_870929.lib").write_text(
        "* solenoid coil model (test fixture)\n"
        ".subckt KIDDE870929 coil_a coil_b\n"
        "L1 coil_a mid 50m\n"
        "R1 mid coil_b 28\n"
        ".ends\n"
    )
    return library


def test_find_model_resolves_by_normalized_part_number(tmp_path):
    from docling_serve.extractors.spice_models import find_model

    library = _model_library(tmp_path)
    model = find_model("KIDDE 870929", library_dir=library)
    assert model is not None
    assert model.name == "KIDDE870929"
    assert model.pin_count == 2
    assert model.is_subckt
    # Punctuation/case drift in the printed number still resolves.
    assert find_model("kidde-870929", library_dir=library) is not None
    assert find_model("UNKNOWN-1", library_dir=library) is None
    assert find_model(None, library_dir=library) is None


def test_graph_to_spice_binds_vendor_models(tmp_path, monkeypatch):
    from docling_serve.extractors.spice import graph_to_spice

    library = _model_library(tmp_path)
    monkeypatch.setenv("DOCLING_SERVE_SPICE_MODEL_DIR", str(library))
    graph = {
        "components": [
            {
                "id": "C1",
                "refDes": "V1",
                "type": "valve",
                "partNumber": "KIDDE 870929",
                "pins": [{"number": "A"}, {"number": "B"}],
            },
            {"id": "C2", "refDes": "K1", "type": "relay", "partNumber": "NOMODEL-1"},
            {"id": "C3", "refDes": "J1", "type": "connector", "partNumber": "CONN-9"},
        ],
        "nets": [
            {"id": "N1", "name": "A8B22", "nodes": [{"component": "C1", "pin": None}, {"component": "C2", "pin": None}]},
            {"id": "N2", "name": "GND", "nodes": [{"component": "C2"}, {"component": "C3"}]},
        ],
    }
    spice = graph_to_spice(graph, source_name="fixture.pdf")
    assert "XV1 A8B22 NC_V1_1 KIDDE870929" in spice  # tier 1: vendor model bound
    assert ".subckt KIDDE870929 coil_a coil_b" in spice  # model text inlined
    assert ".subckt INF_NOMODEL_1_2P" in spice  # tier 2: relay physics inferred
    assert ".subckt SC_CONN_9_2P" in spice  # tier 3: connector keeps its stub

    import shutil as _shutil
    import subprocess

    if _shutil.which("ngspice"):
        checked = spice.replace(".end\n", ".control\nlisting e\nquit\n.endc\n.end\n")
        circuit = tmp_path / "bound.cir"
        circuit.write_text(checked)
        result = subprocess.run(
            ["ngspice", "-b", str(circuit)], capture_output=True, text=True, timeout=60
        )
        combined = (result.stdout + result.stderr).lower()
        assert result.returncode == 0, combined[:400]
        assert "error:" not in combined.replace("no error", ""), combined[:400]


# --------------------------------------------------------------------------- #
# XML export                                                                   #
# --------------------------------------------------------------------------- #


def test_graph_to_xml_full_round_trip():
    import xml.etree.ElementTree as ET

    from docling_serve.extractors.xml_export import graph_to_xml

    graph = {
        "confidence": 0.9,
        "titleBlock": {"title": "Lixie Clock", "drawingNumber": "MSX-001", "revision": "A"},
        "pages": [{"pageNumber": 1}],
        "components": [
            {
                "id": "C0001",
                "refDes": "V1",
                "type": "valve",
                "partNumber": "BDP-000002",
                "location": "RH SIDE STA 21",
                "page": 1,
                "bbox": [100, 150, 290, 260],
                "pins": [{"name": "PIN A", "status": "connected"}, {"name": "PIN B"}],
            },
        ],
        "nets": [
            {
                "id": "N1",
                "name": "A8B22",
                "gauge": "22 AWG",
                "signalType": "signal",
                "page": 1,
                "nodes": [{"component": "C0001", "pin": "PIN B", "attachment": [295.4, 190.0]}],
                "segments": [[295.4, 190.0, 450.0, 190.0]],
            }
        ],
    }
    xml_text = graph_to_xml(graph, source_name="fixture.pdf")
    assert xml_text.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    root = ET.fromstring(xml_text)
    assert root.tag == "schematic"
    assert root.get("source") == "fixture.pdf"
    assert root.find("titleBlock/title").text == "Lixie Clock"
    component = root.find("components/component")
    assert component.get("refDes") == "V1"
    assert component.get("partNumber") == "BDP-000002"
    assert component.find("bbox").get("x1") == "290"
    assert [p.get("name") for p in component.findall("pins/pin")] == ["PIN A", "PIN B"]
    net = root.find("nets/net")
    assert net.get("name") == "A8B22"
    node = net.find("node")
    assert node.get("pin") == "PIN B"
    assert node.get("x") == "295.4"
    assert net.find("segments/segment").get("x2") == "450.0"


# --------------------------------------------------------------------------- #
# Off-page (cross-sheet) net runs                                              #
# --------------------------------------------------------------------------- #


def test_trace_nets_keeps_long_single_component_run_drops_short_artwork():
    """A long run touching one component is an off-page net (continues on
    another sheet); short single-touch clusters remain dropped as artwork."""
    from docling_serve.extractors.net_trace import ComponentBox, trace_nets

    boxes = [ComponentBox(ref="K1", x0=100, y0=100, x1=200, y1=200)]
    long_run = [(200.0, 150.0), (500.0, 150.0)]  # 300 pt to the border
    short_artwork = [(210.0, 190.0), (230.0, 190.0)]  # 20 pt squiggle
    nets = trace_nets([long_run, short_artwork], boxes)
    assert len(nets) == 1
    assert nets[0].components == ["K1"]


# --------------------------------------------------------------------------- #
# Crop-verification of component labels                                        #
# --------------------------------------------------------------------------- #


def test_apply_corrections_rebinds_refdes_and_rewrites_net_refs():
    from docling_serve.extractors.label_verify import apply_corrections

    result = {
        "components": [
            {"refDes": "Z14", "type": "IC", "partNumber": "ATMEGA328P-PU", "bbox": [0, 0, 1, 1]},
            {"refDes": "U3", "type": "IC", "partNumber": "FT232RB-REEL", "bbox": [2, 0, 3, 1]},
            {"refDes": "J1", "type": "Connector", "parentComponent": "Z14", "bbox": [4, 0, 5, 1]},
        ],
        "nets": [
            {"name": "XTAL1", "nodes": [{"refDes": "Z14", "pin": "9"}, {"refDes": "U3", "pin": "1"}]}
        ],
    }
    selected = result["components"]
    verified = {
        0: {"index": 0, "refDes": "ZU4", "partNumber": "ATMEGA328P-PU"},
        # Crop shows the TRUE print — the FT232 was knowledge-substitution.
        1: {"index": 1, "refDes": "U3", "partNumber": "ATMEGA16U2-MU(R)"},
        # Null part numbers never override (crop may simply miss the label).
        2: {"index": 2, "refDes": "J1", "partNumber": None},
    }
    corrections = apply_corrections(result, verified, selected)
    assert corrections >= 2
    assert result["components"][0]["refDes"] == "ZU4"
    assert result["components"][1]["partNumber"] == "ATMEGA16U2-MU(R)"
    # Net node references follow the rename, as does parentComponent.
    assert result["nets"][0]["nodes"][0]["refDes"] == "ZU4"
    assert result["components"][2]["parentComponent"] == "ZU4"


def test_apply_corrections_rejects_implausible_or_colliding_refdes():
    from docling_serve.extractors.label_verify import apply_corrections

    result = {
        "components": [
            {"refDes": "C5", "type": "capacitor", "bbox": [0, 0, 1, 1]},
            {"refDes": "R2", "type": "resistor", "bbox": [2, 0, 3, 1]},
            {"refDes": "U1", "type": "IC", "partNumber": "LP2985-33DBVR", "bbox": [4, 0, 5, 1]},
        ],
        "nets": [],
    }
    selected = result["components"]
    verified = {
        0: {"index": 0, "refDes": "100u", "partNumber": "100u"},  # value, not refDes
        1: {"index": 1, "refDes": "U1", "partNumber": None},  # collides with existing U1
        2: {"index": 2, "refDes": "AREF", "partNumber": None},  # net label
    }
    assert apply_corrections(result, verified, selected) == 0
    assert [c["refDes"] for c in result["components"]] == ["C5", "R2", "U1"]
    assert result["components"][2]["partNumber"] == "LP2985-33DBVR"  # null never overrides


def test_apply_corrections_crop_adoption_replaces_invented_identity():
    """When a complete transcribed pair matches NO existing refDes and NO
    part number, the cropped component adopts it (the whole-page pass invented
    Q2 where the print says SW2 / PS1023ABLK)."""
    from docling_serve.extractors.label_verify import apply_corrections

    result = {
        "components": [
            {"refDes": "Q2", "type": "transistor", "bbox": [0, 0, 1, 1]},
        ],
        "nets": [{"name": "BTN2", "nodes": [{"refDes": "Q2", "pin": "1"}]}],
    }
    verified = {0: {"refDes": "SW2", "partNumber": "PS1023ABLK"}}
    assert apply_corrections(result, verified, result["components"]) == 1
    component = result["components"][0]
    assert component["refDes"] == "SW2"
    assert component["partNumber"] == "PS1023ABLK"
    assert result["nets"][0]["nodes"][0]["refDes"] == "SW2"


def test_apply_corrections_resolves_swap_chains():
    """IC1→U1 while U1→U3: renames must apply as a simultaneous permutation."""
    from docling_serve.extractors.label_verify import apply_corrections

    result = {
        "components": [
            {"refDes": "IC1", "type": "IC", "partNumber": "NCP1117ST50T3G", "bbox": [0, 0, 1, 1]},
            {"refDes": "U1", "type": "IC", "partNumber": "ATMEGA16U2-MU(R)", "bbox": [2, 0, 3, 1]},
        ],
        "nets": [{"name": "+5V", "nodes": [{"refDes": "IC1", "pin": "2"}, {"refDes": "U1", "pin": "4"}]}],
    }
    selected = result["components"]
    verified = {
        0: {"index": 0, "refDes": "U1", "partNumber": "NCP1117ST50T3G"},
        1: {"index": 1, "refDes": "U3", "partNumber": "ATMEGA16U2-MU(R)"},
    }
    assert apply_corrections(result, verified, selected) == 2
    assert [c["refDes"] for c in result["components"]] == ["U1", "U3"]
    nodes = result["nets"][0]["nodes"]
    assert [n["refDes"] for n in nodes] == ["U1", "U3"]


def test_apply_corrections_displaces_unverified_occupant():
    """A verified (refDes, part) pair beats the whole-page binding: the
    component holding ATMEGA16U2 takes the printed name U3 and the unverified
    occupant of U3 is displaced into the vacated designator."""
    from docling_serve.extractors.label_verify import apply_corrections

    result = {
        "components": [
            {"refDes": "U1", "type": "IC", "partNumber": "ATMEGA16U2-MU(R)", "bbox": [0, 0, 1, 1]},
            {"refDes": "U3", "type": "IC", "partNumber": "FT232RG-33SSOP", "bbox": [2, 0, 3, 1]},
        ],
        "nets": [{"name": "D+", "nodes": [{"refDes": "U1", "pin": "1"}, {"refDes": "U3", "pin": "2"}]}],
    }
    verified = {0: {"refDes": "U3", "partNumber": "ATMEGA16U2-MU(R)"}}
    assert apply_corrections(result, verified, result["components"]) == 2
    assert [c["refDes"] for c in result["components"]] == ["U3", "U1"]
    assert [n["refDes"] for n in result["nets"][0]["nodes"]] == ["U3", "U1"]


def test_select_components_keeps_significant_boxed_only():
    from docling_serve.extractors.label_verify import (
        MAX_VERIFY_COMPONENTS,
        select_components,
    )

    components = [{"refDes": f"R{i}", "type": "resistor", "bbox": [0, 0, 1, 1]} for i in range(40)]
    components.append({"refDes": "U1", "type": "IC", "bbox": [0, 0, 1, 1]})
    components.append({"refDes": "X9", "type": "capacitor", "partNumber": "PN-1", "bbox": [0, 0, 1, 1]})
    components.append({"refDes": "NOBOX", "type": "IC"})  # unboxed: skipped
    selected = select_components(components)
    assert [c["refDes"] for c in selected] == ["U1", "X9"]  # significant + boxed only
    ics = [{"refDes": f"U{i}", "type": "IC", "bbox": [0, 0, 1, 1]} for i in range(40)]
    assert len(select_components(ics)) == MAX_VERIFY_COMPONENTS


# --------------------------------------------------------------------------- #
# Model-response cache (run-to-run determinism)                                #
# --------------------------------------------------------------------------- #


class _FakeVisionProvider:
    vision_model = "fake-model"

    def __init__(self):
        self.calls = 0

    def understand_json(self, *, prompt, images, system):
        self.calls += 1
        return {"components": [], "call": self.calls}


def test_cached_understand_json_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCLING_SCHEMATIC_MODEL_CACHE", str(tmp_path))
    provider = _FakeVisionProvider()
    first, cached_first = _cached_understand_json(
        provider, prompt="p", system="s", png_bytes=b"img"
    )
    second, cached_second = _cached_understand_json(
        provider, prompt="p", system="s", png_bytes=b"img"
    )
    assert (cached_first, cached_second) == (False, True)
    assert first == second  # identical result, no second model call
    assert provider.calls == 1
    # A different image (or prompt/model) busts the cache.
    _, cached_third = _cached_understand_json(
        provider, prompt="p", system="s", png_bytes=b"other"
    )
    assert cached_third is False
    assert provider.calls == 2


def test_cached_understand_json_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DOCLING_SCHEMATIC_MODEL_CACHE", "off")
    provider = _FakeVisionProvider()
    for _ in range(2):
        _, cached = _cached_understand_json(provider, prompt="p", system="s", png_bytes=b"x")
        assert cached is False
    assert provider.calls == 2


# --------------------------------------------------------------------------- #
# Detection-pass merge (second "box every symbol" model pass)                  #
# --------------------------------------------------------------------------- #


def test_scale_bboxes_to_pt_uses_model_image_size():
    result = {
        "imageSize": {"w": 100, "h": 50},
        "components": [{"refDes": "R1", "bbox": [10, 10, 20, 20]}],
    }
    assert _scale_bboxes_to_pt(result, 1000.0, 500.0) is True
    assert result["components"][0]["bboxPt"] == [100.0, 100.0, 200.0, 200.0]
    assert _scale_bboxes_to_pt({"imageSize": {}, "components": []}, 1000.0, 500.0) is False


def test_merge_detected_components_dedupes_and_adds():
    components = [
        {"refDes": "R1", "type": "resistor", "bboxPt": [0.0, 0.0, 10.0, 10.0]},
        {"refDes": "R2", "type": "resistor"},  # main pass forgot the box
    ]
    detected = [
        # Same refDes, box attaches to the main component.
        {"refDes": "R2", "type": "resistor", "bboxPt": [20.0, 0.0, 30.0, 10.0]},
        # Overlaps R1 heavily: same symbol, skipped.
        {"refDes": None, "type": "resistor", "bboxPt": [1.0, 1.0, 11.0, 11.0]},
        # Genuinely new, anonymous: appended with a unique description.
        {"refDes": None, "type": "capacitor", "bboxPt": [50.0, 0.0, 60.0, 10.0]},
        {"refDes": None, "type": "capacitor", "bboxPt": [70.0, 0.0, 80.0, 10.0]},
    ]
    merged = _merge_detected_components(components, detected)
    assert merged == 3  # R2 box + two new capacitors
    assert components[1]["bboxPt"] == [20.0, 0.0, 30.0, 10.0]
    added = [c for c in components if c.get("detectedOnly")]
    assert len(added) == 2
    # Anonymous descriptions stay unique so net nodes don't collapse.
    assert len({c["description"] for c in added}) == 2


# --------------------------------------------------------------------------- #
# SVG geometry -> KiCad schematic (.kicad_sch)                                 #
# --------------------------------------------------------------------------- #

_SVG_HEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'width="72" height="72" viewBox="0 0 72 72">'
)


def test_svg_to_kicad_sch_emits_polylines_in_mm():
    svg = (
        _SVG_HEADER
        + '<path fill="none" stroke="rgb(0%, 0%, 0%)" stroke-width="1" '
        'd="M 0 0 L 72 72"/></svg>'
    )
    sch = svg_to_kicad_sch(svg, title="unit")
    assert sch.startswith("(kicad_sch")
    # 72pt page -> 25.4mm user paper, endpoints scaled pt->mm.
    assert '(paper "User" 25.4 25.4)' in sch
    assert "(polyline (pts (xy 0 0) (xy 25.4 25.4))" in sch
    assert '(title_block (title "unit"))' in sch
    assert sch.count("(") == sch.count(")")


def test_svg_to_kicad_sch_expands_glyph_uses_and_skips_background():
    svg = (
        _SVG_HEADER
        + '<defs><g id="glyph-0-0">'
        '<path d="M 0 0 L 2 0 L 2 2 Z"/></g></defs>'
        # Full-page background fill must not become a polyline.
        '<path fill="rgb(100%, 100%, 100%)" d="M 0 0 L 0 72 L 72 72 L 72 0 Z"/>'
        '<g fill="rgb(0%, 0%, 0%)"><use xlink:href="#glyph-0-0" x="10" y="10"/></g>'
        "</svg>"
    )
    sch = svg_to_kicad_sch(svg)
    assert sch.count("(polyline") == 1
    # Glyph translated by (10, 10)pt then scaled to mm; closed outline filled.
    assert "(xy 3.528 3.528)" in sch
    assert "(fill (type outline))" in sch


def test_svg_to_kicad_sch_applies_matrix_transforms():
    svg = (
        _SVG_HEADER
        + '<path fill="none" stroke="rgb(0%, 0%, 0%)" stroke-width="0.5" '
        'transform="matrix(0, 1, 1, 0, 10, 0)" d="M 0 0 L 0 20"/></svg>'
    )
    sch = svg_to_kicad_sch(svg)
    # (0,0)->(10,0) and (0,20)->(30,0) in pt, then *25.4/72.
    assert "(xy 3.528 0) (xy 10.583 0)" in sch
    # Local stroke width scaled by the (unit-scale) matrix.
    assert "(stroke (width 0.176)" in sch


def test_svg_to_kicad_sch_flattens_curves():
    svg = (
        _SVG_HEADER
        + '<path fill="none" stroke="rgb(0%, 0%, 0%)" '
        'd="M 0 36 C 24 0 48 72 72 36"/></svg>'
    )
    sch = svg_to_kicad_sch(svg)
    points = sch.count("(xy ")
    assert points > 4  # curve became multiple short segments
    assert "C " not in sch


def test_svg_to_kicad_sch_rejects_invalid_svg():
    with pytest.raises(KicadConversionError):
        svg_to_kicad_sch("not an svg document")


def test_parse_path_data_subpaths_and_close():
    subpaths = parse_path_data("M 0 0 L 1 0 L 1 1 Z M 5 5 L 6 5")
    assert len(subpaths) == 2
    first, closed_first = subpaths[0]
    second, closed_second = subpaths[1]
    assert closed_first is True and closed_second is False
    assert first[0] == (0.0, 0.0) and second == [(5.0, 5.0), (6.0, 5.0)]


# --------------------------------------------------------------------------- #
# Access extractor (mdbtools mocked)                                          #
# --------------------------------------------------------------------------- #


def test_access_extractor_builds_units_and_csv(tmp_path, monkeypatch):
    db = tmp_path / "inventory.accdb"
    db.write_bytes(b"fake-access")
    ctx = _ctx(tmp_path, "inventory.accdb")
    ctx.source_path = db  # real bytes on disk

    monkeypatch.setattr(access_mod, "mdbtools_available", lambda: True)
    monkeypatch.setattr(access_mod, "list_tables", lambda p: ["Parts"])
    monkeypatch.setattr(
        access_mod,
        "export_table",
        lambda p, t: (["id", "name"], [["1", "bolt"], ["2", "nut"]]),
    )
    monkeypatch.setattr(access_mod, "dump_schema", lambda p: "CREATE TABLE Parts (...);")

    result = access_mod.AccessExtractor().build(ctx)

    assert result.extractor == "extract_access"
    assert result.domain == "database"
    assert result.structured["database"]["tableCount"] == 1
    assert result.structured["database"]["rowCount"] == 2

    csv_path = ctx.bundle_dir / "tables" / "Parts.csv"
    assert csv_path.is_file()
    assert "bolt" in csv_path.read_text()
    assert (ctx.bundle_dir / "access-tables.json").is_file()
    assert (ctx.bundle_dir / "access-schema.sql").is_file()


def test_access_extractor_requires_mdbtools(tmp_path, monkeypatch):
    db = tmp_path / "x.accdb"
    db.write_bytes(b"x")
    ctx = _ctx(tmp_path, "x.accdb")
    ctx.source_path = db
    monkeypatch.setattr(access_mod, "mdbtools_available", lambda: False)
    with pytest.raises(access_mod.AccessToolsUnavailableError):
        access_mod.AccessExtractor().build(ctx)


# --------------------------------------------------------------------------- #
# Image-context enhancer (Bedrock mocked)                                     #
# --------------------------------------------------------------------------- #


class _FakeProvider:
    enabled = True
    vision_model = "fake-vision"

    def converse(self, *, messages, **_):
        return f"context for {len(messages[0].images)} image(s)"


def test_image_context_enhancer_writes_context(tmp_path, monkeypatch):
    from docling_serve.extractors.base import ExtractorResult
    from docling_serve.extractors.enhancers import image_context as ic, run_enhancements

    monkeypatch.setattr(ic, "get_bedrock_provider", lambda: _FakeProvider())

    ctx = _ctx(tmp_path, "deck.pptx", enhancements=["image_context"])
    media = ctx.bundle_dir / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "img1.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")

    document = {
        "assets": [{"assetId": "a1", "path": "media/img1.png", "contentType": "image/png"}],
        "document": {
            "units": [
                {"content": {"elements": [{"type": "image", "assetRef": "a1"}]}}
            ]
        },
    }
    base = ExtractorResult(structured=document, extractor="extract_ppt")

    results = run_enhancements(ctx, document, base_result=base)

    assert len(results) == 1 and results[0].applied is True
    assert document["assets"][0]["context"].startswith("context for 1")
    element = document["document"]["units"][0]["content"]["elements"][0]
    assert element["context"].startswith("context for 1")

    sidecar = ctx.bundle_dir / "image-context.json"
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text())["count"] == 1


def test_image_context_enhancer_skips_when_disabled(tmp_path, monkeypatch):
    from docling_serve.extractors.base import ExtractorResult
    from docling_serve.extractors.enhancers import image_context as ic, run_enhancements

    class _Disabled:
        enabled = False
        vision_model = "fake"

    monkeypatch.setattr(ic, "get_bedrock_provider", lambda: _Disabled())
    ctx = _ctx(tmp_path, "deck.pptx", enhancements=["image_context"])
    document = {"assets": [{"assetId": "a1", "path": "media/x.png", "contentType": "image/png"}]}
    base = ExtractorResult(structured=document, extractor="extract_ppt")
    assert run_enhancements(ctx, document, base_result=base) == []


# --------------------------------------------------------------------------- #
# Connectors                                                                  #
# --------------------------------------------------------------------------- #


def test_file_connector_yields_items_from_bytes_and_paths(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    items = list(
        FileConnector().discover(
            {
                "files": [{"name": "a.txt", "data": b"hello", "profile": "default"}],
                "paths": [str(f)],
                "profile": "schematic",
            }
        )
    )
    assert {i.name for i in items} == {"a.txt", "doc.pdf"}
    by_name = {i.name: i for i in items}
    assert by_name["a.txt"].read_bytes() == b"hello"
    assert by_name["doc.pdf"].read_bytes() == b"%PDF-1.4"
    assert by_name["doc.pdf"].suggested_profile == "schematic"


def test_resolve_connector_and_registry():
    assert "file" in available_connectors()
    assert resolve_connector("accessdb").name == "accessdb"
    assert resolve_connector("access").name == "accessdb"  # alias
    assert {"s3", "textract"}.issubset(set(available_services()))
    with pytest.raises(ConnectorError):
        resolve_connector("does-not-exist")


# --------------------------------------------------------------------------- #
# Knowledge-graph enhancer (docling-graph mocked)                             #
# --------------------------------------------------------------------------- #


class _FakeGraph:
    """Minimal NetworkX-DiGraph stand-in: just nodes(data=)/edges(data=)."""

    def __init__(self, nodes, edges):
        self._nodes = nodes  # list[(id, attrs)]
        self._edges = edges  # list[(u, v, attrs)]

    def nodes(self, data=False):
        return self._nodes if data else [n for n, _ in self._nodes]

    def edges(self, data=False):
        return self._edges if data else [(u, v) for u, v, _ in self._edges]


def test_graph_to_payload_serializes_nodes_and_edges():
    from docling_serve.extractors.enhancers.graph_extraction import _graph_to_payload

    graph = _FakeGraph(
        nodes=[
            ("R1", {"id": "R1", "label": "Component", "type": "entity",
                    "__class__": "Component", "kind": "resistor", "value": "10k"}),
            ("GND", {"id": "GND", "label": "Net", "type": "entity", "name": "GND"}),
        ],
        edges=[("GND", "R1", {"label": "members"})],
    )
    payload = _graph_to_payload(graph)

    assert payload["labels"] == {"Component": 1, "Net": 1}
    assert payload["edgeLabels"] == {"members": 1}
    r1 = next(n for n in payload["nodes"] if n["id"] == "R1")
    assert r1["properties"] == {"kind": "resistor", "value": "10k"}
    assert payload["edges"][0] == {
        "source": "GND", "target": "R1", "label": "members", "properties": {},
    }


def test_graph_enhancer_writes_sidecar(tmp_path, monkeypatch):
    from docling_serve.extractors.base import ExtractorResult
    from docling_serve.extractors.enhancers import (
        graph_extraction as ge,
        run_enhancements,
    )
    from docling_serve.settings import docling_serve_settings

    monkeypatch.setattr(docling_serve_settings, "graph_litellm_base_url", "http://127.0.0.1:4000/v1")
    monkeypatch.setattr(docling_serve_settings, "graph_litellm_api_key", "sk-test")
    # Pretend docling-graph is installed; mock the actual run.
    monkeypatch.setattr(ge.importlib.util, "find_spec", lambda name: object())
    fake = _FakeGraph(
        nodes=[
            ("SCH-1", {"label": "WiringDiagram", "type": "entity", "title": "SCH-1"}),
            ("R1", {"label": "Component", "type": "entity", "kind": "resistor"}),
        ],
        edges=[("SCH-1", "R1", {"label": "components"})],
    )
    monkeypatch.setattr(ge, "run_graph_extraction", lambda src, cfg: (fake, 1))

    ctx = _ctx(tmp_path, "doc.pdf", enhancements=["knowledge_graph"])
    (ctx.bundle_dir / "document.md").write_text("# SCH-1\nR1 is a resistor.", encoding="utf-8")
    document: dict = {"document": {"units": []}}
    base = ExtractorResult(structured=document, extractor="extract_text")

    results = run_enhancements(ctx, document, base_result=base)

    assert len(results) == 1 and results[0].applied is True
    kg = document["knowledgeGraph"]
    assert kg["nodeCount"] == 2 and kg["edgeCount"] == 1
    sidecar = ctx.bundle_dir / "knowledge-graph.json"
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text())
    assert data["labels"] == {"WiringDiagram": 1, "Component": 1}
    assert data["edgeLabels"] == {"components": 1}


def test_graph_enhancer_skips_when_unconfigured(tmp_path, monkeypatch):
    from docling_serve.extractors.base import ExtractorResult
    from docling_serve.extractors.enhancers import run_enhancements
    from docling_serve.settings import docling_serve_settings

    monkeypatch.setattr(docling_serve_settings, "graph_litellm_base_url", None)
    monkeypatch.setattr(docling_serve_settings, "graph_litellm_api_key", None)

    ctx = _ctx(tmp_path, "doc.pdf", enhancements=["knowledge_graph"])
    (ctx.bundle_dir / "document.md").write_text("hi", encoding="utf-8")
    document: dict = {}
    base = ExtractorResult(structured=document, extractor="extract_text")

    assert run_enhancements(ctx, document, base_result=base) == []


def test_graph_payload_from_text_returns_payload(monkeypatch):
    from docling_serve.extractors.enhancers import graph_extraction as ge
    from docling_serve.settings import docling_serve_settings

    monkeypatch.setattr(docling_serve_settings, "graph_litellm_base_url", "http://127.0.0.1:4000/v1")
    monkeypatch.setattr(docling_serve_settings, "graph_litellm_api_key", "sk-test")
    monkeypatch.setattr(ge.importlib.util, "find_spec", lambda name: object())
    fake = _FakeGraph(
        nodes=[
            ("DOC", {"label": "DocumentGraph", "type": "entity", "title": "DOC"}),
            ("Acme", {"label": "Entity", "type": "entity", "name": "Acme"}),
        ],
        edges=[("DOC", "Acme", {"label": "entities"})],
    )
    monkeypatch.setattr(ge, "run_graph_extraction", lambda src, cfg: (fake, 1))

    payload = ge.graph_payload_from_text("Acme builds widgets.")

    assert payload["nodeCount"] == 2 and payload["edgeCount"] == 1
    assert payload["model"]["provider"] == "litellm_proxy"
    assert {n["label"] for n in payload["nodes"]} == {"DocumentGraph", "Entity"}


def test_graph_payload_from_text_raises_when_unconfigured(monkeypatch):
    from docling_serve.extractors.enhancers import graph_extraction as ge
    from docling_serve.settings import docling_serve_settings

    monkeypatch.setattr(ge.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(docling_serve_settings, "graph_litellm_base_url", None)
    monkeypatch.setattr(docling_serve_settings, "graph_litellm_api_key", None)

    with pytest.raises(ge.GraphExtractionUnavailable):
        ge.graph_payload_from_text("some text")


def test_normalize_graph_merges_components_and_named_nets_across_pages():
    """Cross-page identity: the same refDes on two sheets is ONE component (pins
    unioned, pages recorded); nets sharing a label merge into one electrical net;
    unnamed nets stay page-local."""
    page1 = {
        "__page__": 1,
        "titleBlock": {"title": "PSU", "sheet": "1/2"},
        "components": [
            {"refDes": "K1", "type": "relay", "pins": [{"number": "1", "name": "COIL+"}]},
            {"refDes": "R1", "type": "resistor", "pins": [{"number": "1", "name": None}]},
        ],
        "nets": [
            {"name": "GND", "nodes": [{"refDes": "R1", "pin": "1"}]},
            {"name": None, "nodes": [{"refDes": "K1", "pin": "1"}, {"refDes": "R1", "pin": "1"}]},
        ],
        "confidence": 0.9,
        "warnings": [],
    }
    page2 = {
        "__page__": 2,
        "titleBlock": {"title": "PSU", "sheet": "2/2"},
        "components": [
            # Same relay, second sheet: new pin + a value the first sighting lacked.
            {"refDes": "k1", "type": None, "value": "28V", "pins": [{"number": "2", "name": "COIL-"}]},
            {"refDes": "C1", "type": "capacitor", "pins": [{"number": "1", "name": None}]},
        ],
        "nets": [
            {"name": "gnd", "nodes": [{"refDes": "C1", "pin": "1"}]},
            {"name": "+28V", "nodes": [{"refDes": "k1", "pin": "2"}]},
        ],
        "confidence": 0.8,
        "warnings": [],
    }
    graph = _normalize_graph(
        [page1, page2],
        source_name="psu.pdf",
        model_id="test-model",
        understood=True,
        svg_paths=[],
        page_images=[],
        warnings=[],
    )

    # K1 merged: one component, both pages, pins unioned, value backfilled.
    k1 = [c for c in graph["components"] if (c["refDes"] or "").upper() == "K1"]
    assert len(k1) == 1
    assert k1[0]["pages"] == [1, 2]
    assert {p["name"] for p in k1[0]["pins"]} == {"COIL+", "COIL-"}
    assert k1[0]["value"] == "28V"
    assert k1[0]["type"] == "relay"

    # GND merged across pages (case-insensitive): one net with both nodes.
    gnds = [n for n in graph["nets"] if (n["name"] or "").upper() == "GND"]
    assert len(gnds) == 1
    ids = {c["refDes"]: c["id"] for c in graph["components"] if c["refDes"]}
    assert {node["component"] for node in gnds[0]["nodes"]} == {ids["R1"], ids["C1"]}

    # The unnamed page-1 net stays separate; +28V resolves k1 -> K1's id.
    assert sum(1 for n in graph["nets"] if not n["name"]) == 1
    plus28 = next(n for n in graph["nets"] if n["name"] == "+28V")
    assert plus28["nodes"][0]["component"] == k1[0]["id"]

    validate_artifact(graph, "schematic-graph.schema.json")


def test_profile_template_resolution():
    """Profiles map to domain templates; unknown/empty profiles use the default."""
    from docling_serve.extractors.enhancers import resolve_profile_template

    assert resolve_profile_template("schematic").endswith("SchematicDocumentGraph")
    assert resolve_profile_template("DRAWING").endswith("SchematicDocumentGraph")
    assert resolve_profile_template("access").endswith("AccessDatabaseGraph")
    assert resolve_profile_template("default") is None
    assert resolve_profile_template(None) is None

    # The mapped templates import cleanly (the extractor's _import_template path).
    from docling_serve.extractors.enhancers.graph_extraction import _import_template

    for profile in ("schematic", "access"):
        cls = _import_template(resolve_profile_template(profile))
        assert cls.model_config.get("is_entity") is True


def test_import_template_rejects_arbitrary_module():
    """A request-supplied template path must be allow-listed (no arbitrary import)."""
    from docling_serve.extractors.enhancers.graph_extraction import (
        GraphExtractionUnavailable,
        _import_template,
    )

    for malicious in ("os.system", "subprocess.run", "builtins.eval"):
        with pytest.raises(GraphExtractionUnavailable, match="template_not_allowed"):
            _import_template(malicious)


def test_import_template_allows_configured_override(monkeypatch):
    from docling_serve.extractors.enhancers import graph_extraction as ge

    override = "docling_serve.extractors.enhancers.graph_templates.SchematicDocumentGraph"
    monkeypatch.setattr(
        ge.docling_serve_settings, "graph_extraction_template", override
    )
    assert ge._import_template(override).model_config.get("is_entity") is True


def test_usaf_sustainment_profile_template():
    from docling_serve.extractors.enhancers import resolve_profile_template
    from docling_serve.extractors.enhancers.graph_extraction import _import_template

    for profile in ("usaf-sustainment", "sustainment", "USAF"):
        path = resolve_profile_template(profile)
        assert path.endswith("UsafSustainmentGraph")
    cls = _import_template(resolve_profile_template("usaf-sustainment"))
    assert cls.model_config.get("is_entity") is True
    # The type steering must carry the domain vocabulary.
    type_desc = cls.model_fields["entities"].annotation.__args__[0].model_fields["type"].description
    for required in ("WeaponSystem", "EndItem", "AssistanceRequest", "ITSystem"):
        assert required in type_desc


# --------------------------------------------------------------------------- #
# Schematic revision (browser edits) + delivery checks                         #
# --------------------------------------------------------------------------- #


def _revision_graph():
    return {
        "schemaVersion": "1",
        "artifactKind": "captify.schematic.v1",
        "source": {"originalFileName": "fixture.pdf", "fileKind": "schematic"},
        "model": {"provider": "bedrock", "modelId": "test", "understood": True},
        "pages": [{"page": 1, "width": 612, "height": 792}],
        "components": [
            {"id": "C1", "refDes": "R1", "type": "resistor", "value": "10k",
             "bbox": [10, 10, 30, 20], "page": 1},
            {"id": "C2", "refDes": "V1", "type": "valve", "partNumber": "WRONG-1",
             "bbox": [50, 10, 80, 30], "page": 1},
            {"id": "C3", "refDes": "X9", "type": "artifact", "page": 1},
        ],
        "nets": [
            {"id": "N1", "name": "A8B22", "page": 1,
             "segments": [[30, 15, 50, 15]],
             "nodes": [{"component": "C1", "pin": None}, {"component": "C2", "pin": None}]},
            {"id": "N2", "name": None, "page": 1, "segments": [],
             "nodes": [{"component": "C3", "pin": None}]},
        ],
        "confidence": 0.9,
        "warnings": [],
        "notes": [],
    }


def test_apply_graph_edits_updates_deletes_and_bumps_revision():
    from docling_serve.extractors.schematic_revision import apply_graph_edits

    graph = _revision_graph()
    applied = apply_graph_edits(graph, {
        "components": [
            {"id": "C2", "partNumber": "KIDDE 870929", "refDes": "V2"},
            {"id": "C3", "delete": True},
            {"id": "MISSING", "refDes": "nope"},
        ],
        "nets": [
            {"id": "N2", "delete": True},
            {"id": "N1", "name": "A8B23"},
        ],
    })
    assert applied == {"componentEdits": 1, "componentDeletes": 1,
                       "netEdits": 1, "netDeletes": 1}
    assert graph["revision"] == 1
    ids = [c["id"] for c in graph["components"]]
    assert ids == ["C1", "C2"]
    edited = graph["components"][1]
    assert edited["partNumber"] == "KIDDE 870929" and edited["refDes"] == "V2"
    assert [n["id"] for n in graph["nets"]] == ["N1"]
    assert graph["nets"][0]["name"] == "A8B23"


def test_graph_integrity_checks_flag_issues():
    from docling_serve.extractors.schematic_revision import check_graph_integrity

    graph = _revision_graph()
    results = {c.id: c for c in check_graph_integrity(graph)}
    assert results["schema"].status == "pass"
    # C3 has no bbox -> warn; N2 is single-ended -> warn.
    assert results["components"].status == "warn"
    assert results["connectivity"].status == "warn"
    assert results["references"].status == "pass"

    # Delete the artifact + dangling net: everything greens.
    from docling_serve.extractors.schematic_revision import apply_graph_edits

    apply_graph_edits(graph, {"components": [{"id": "C3", "delete": True}],
                              "nets": [{"id": "N2", "delete": True}]})
    results = {c.id: c for c in check_graph_integrity(graph)}
    assert results["components"].status == "pass"
    assert results["connectivity"].status == "pass"


def test_strip_injected_items_removes_only_electrical_items():
    from docling_serve.extractors.kicad_sch import (
        inject_items,
        net_wires_sexpr,
        svg_to_kicad_sch,
    )
    from docling_serve.extractors.schematic_revision import _strip_injected_items

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100pt" height="100pt" '
        'viewBox="0 0 100 100"><path d="M 10 10 L 90 10" '
        'style="fill:none;stroke:#000000;stroke-width:1"/></svg>'
    )
    base = svg_to_kicad_sch(svg, title="strip-test")
    nets = [{"id": "N1", "page": 1, "segments": [[10, 10, 90, 10]], "nodes": []}]
    injected = inject_items(base, net_wires_sexpr(nets, page_no=1))
    assert "(wire (pts" in injected
    stripped = _strip_injected_items(injected)
    assert "(wire (pts" not in stripped
    # Geometry replay polyline survives the strip.
    assert stripped.count("(polyline") == base.count("(polyline")
    # Idempotent: stripping a clean document changes nothing.
    assert _strip_injected_items(stripped) == stripped


def test_run_delivery_checks_on_generated_artifacts(tmp_path):
    """End-to-end check suite over real serializer outputs (tool-validated)."""
    from docling_serve.extractors.edml import graph_to_edml
    from docling_serve.extractors.kbl import graph_to_kbl
    from docling_serve.extractors.kicad_sch import (
        inject_items,
        net_wires_sexpr,
        svg_to_kicad_sch,
    )
    from docling_serve.extractors.netlist import graph_to_kicad_netlist
    from docling_serve.extractors.schematic_revision import run_delivery_checks
    from docling_serve.extractors.spice import graph_to_spice
    from docling_serve.extractors.xml_export import graph_to_xml

    graph = _revision_graph()
    (tmp_path / "fixture.net").write_text(graph_to_kicad_netlist(graph, source_name="fixture.pdf"))
    (tmp_path / "fixture.edml").write_text(graph_to_edml(graph, source_name="fixture.pdf"))
    (tmp_path / "fixture.xml").write_text(graph_to_xml(graph, source_name="fixture.pdf"))
    (tmp_path / "fixture.kbl").write_text(graph_to_kbl(graph, source_name="fixture.pdf"))
    (tmp_path / "fixture.cir").write_text(graph_to_spice(graph, source_name="fixture.pdf"))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="612pt" height="792pt" '
        'viewBox="0 0 612 792"><path d="M 30 15 L 50 15" '
        'style="fill:none;stroke:#000000;stroke-width:1"/></svg>'
    )
    base = svg_to_kicad_sch(svg, title="fixture")
    (tmp_path / "schematic.kicad_sch").write_text(
        inject_items(base, net_wires_sexpr(graph["nets"], page_no=1))
    )

    results = {c.id: c for c in run_delivery_checks(graph, tmp_path)}
    assert results["schema"].status == "pass"
    assert results["netlist"].status == "pass"
    assert results["xml"].status == "pass"
    assert results["kbl"].status in {"pass", "skip"}
    assert results["kicad"].status in {"pass", "skip"}
    assert results["spice"].status in {"pass", "skip"}
    # No fail across the suite for a freshly-generated bundle.
    assert all(c.status != "fail" for c in results.values()), {
        k: (v.status, v.detail) for k, v in results.items()
    }


def test_find_model_prefers_tenant_scoped_models(tmp_path):
    from docling_serve.extractors.spice_models import find_model

    shared = tmp_path / "KIDDE_870929.lib"
    shared.write_text(".subckt SHARED a b\nR1 a b 1\n.ends\n")
    tenant_dir = tmp_path / "tenants" / "anautics"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "KIDDE_870929.lib").write_text(".subckt TENANT a b\nR1 a b 2\n.ends\n")

    shared_hit = find_model("KIDDE 870929", library_dir=tmp_path)
    assert shared_hit is not None and shared_hit.name == "SHARED"
    tenant_hit = find_model("KIDDE 870929", library_dir=tmp_path, tenant_id="anautics")
    assert tenant_hit is not None and tenant_hit.name == "TENANT"
    # Another tenant cannot see anautics' model but still gets the shared one.
    other = find_model("KIDDE 870929", library_dir=tmp_path, tenant_id="other")
    assert other is not None and other.name == "SHARED"


def test_hierarchy_root_links_pages_and_plots_in_kicad(tmp_path):
    """Multi-page export gets a root document KiCad opens as one hierarchy."""
    import shutil as _shutil
    import subprocess
    import tempfile

    from docling_serve.extractors.kicad_sch import (
        hierarchy_root_sexpr,
        svg_to_kicad_sch,
    )

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100pt" height="100pt" '
        'viewBox="0 0 100 100"><path d="M 10 10 L 90 10" '
        'style="fill:none;stroke:#000000;stroke-width:1"/></svg>'
    )
    pages = []
    for index in (1, 2):
        name = f"schematic-page-{index:03d}.kicad_sch"
        (tmp_path / name).write_text(svg_to_kicad_sch(svg, title=f"page {index}"))
        pages.append(name)
    root = tmp_path / "schematic-root.kicad_sch"
    root.write_text(hierarchy_root_sexpr(pages, title="multisheet"))
    text = root.read_text()
    assert text.count("(sheet (at") == 2
    assert all(f'"Sheetfile" "{page}"' in text for page in pages)

    if not _shutil.which("kicad-cli"):
        pytest.skip("kicad-cli not installed")
    with tempfile.TemporaryDirectory() as out:
        result = subprocess.run(
            ["kicad-cli", "sch", "export", "svg", "--output", out, str(root)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr[:400]
        # The hierarchy plots the root AND its subsheets.
        from pathlib import Path as _P

        produced = sorted(_P(out).glob("*.svg"))
        assert len(produced) >= 3, [p.name for p in produced]


def test_graph_to_spice_wraps_primitive_model_cards(tmp_path, monkeypatch):
    """A vendor .model card (PMOS) binds through a generated wrapper subckt."""
    import shutil as _shutil
    import subprocess

    library = tmp_path / "models"
    library.mkdir()
    (library / "IRF9530.lib").write_text(".model IRF9530 PMOS(VTO=-3.7 KP=8)\n")
    monkeypatch.setenv("DOCLING_SERVE_SPICE_MODEL_DIR", str(library))
    from docling_serve.extractors.spice import graph_to_spice

    graph = {
        "components": [
            {"id": "C1", "refDes": "Q1", "type": "Transistor", "partNumber": "IRF9530"},
        ],
        "nets": [
            {"id": "N1", "name": "D", "nodes": [{"component": "C1"}]},
            {"id": "N2", "name": "G", "nodes": [{"component": "C1"}]},
            {"id": "N3", "name": "S", "nodes": [{"component": "C1"}]},
        ],
    }
    spice = graph_to_spice(graph, source_name="fixture.pdf")
    assert "XQ1 D G S MDL_IRF9530_3P" in spice
    assert ".subckt MDL_IRF9530_3P p1 p2 p3" in spice
    assert "M1 p1 p2 p3 p3 IRF9530" in spice  # bulk tied to source
    assert ".model IRF9530 PMOS" in spice  # vendor card inlined

    if _shutil.which("ngspice"):
        checked = spice.replace(".end\n", ".control\nlisting e\nquit\n.endc\n.end\n")
        circuit = tmp_path / "wrapped.cir"
        circuit.write_text(checked)
        result = subprocess.run(
            ["ngspice", "-b", str(circuit)], capture_output=True, text=True, timeout=60
        )
        combined = (result.stdout + result.stderr).lower()
        assert result.returncode == 0, combined[:400]
        assert "error:" not in combined.replace("no error", ""), combined[:400]


# --------------------------------------------------------------------------- #
# Schematic simulation (classification + real ngspice DC solve)                #
# --------------------------------------------------------------------------- #


def test_classify_schematic_kinds():
    from docling_serve.extractors.spice_simulation import classify_schematic

    electromech = {"components": [
        {"type": "Relay"}, {"type": "Valve/Solenoid"}, {"type": "Switch"},
        {"type": "Fuse"}, {"type": "resistor"},
    ]}
    assert classify_schematic(electromech).kind == "electromechanical-power"
    digital = {"components": [
        {"type": "IC"}, {"type": "Display"}, {"type": "ic"},
        {"type": "resistor"}, {"type": "resistor"}, {"type": "capacitor"},
    ]}
    assert classify_schematic(digital).kind == "digital-logic"
    analog = {"components": [{"type": "transistor"}, {"type": "capacitor"}]}
    assert classify_schematic(analog).kind == "analog-mixed"


def test_detect_power_nets_mil_wire_codes_and_rails():
    from docling_serve.extractors.spice_simulation import detect_power_nets

    graph = {"nets": [
        {"id": "N1", "name": "28VDC BUS"},
        {"id": "N2", "name": "A68A20N"},   # MIL-W-5088 ground wire
        {"id": "N3", "name": "VCC"},
        {"id": "N4", "name": "GND"},
        {"id": "N5", "name": "A12C18"},    # plain signal wire
    ]}
    supplies, grounds = detect_power_nets(graph)
    assert {s["net"]: s["volts"] for s in supplies} == {"N1": 28.0, "N3": None}
    assert grounds == ["N2", "N4"]


def test_simulate_graph_solves_a_divider(tmp_path):
    """Deterministic physics: 28V across two equal coils reads 14V midpoint."""
    import shutil as _shutil

    if not _shutil.which("ngspice"):
        pytest.skip("ngspice not installed")
    from docling_serve.extractors.spice_simulation import simulate_graph

    graph = {
        "components": [
            {"id": "C1", "refDes": "K1", "type": "relay"},
            {"id": "C2", "refDes": "K2", "type": "relay"},
        ],
        "nets": [
            {"id": "N1", "name": "BUS28", "nodes": [{"component": "C1"}]},
            {"id": "N2", "name": "MID", "nodes": [{"component": "C1"}, {"component": "C2"}]},
            {"id": "N3", "name": "RTN", "nodes": [{"component": "C2"}]},
        ],
    }
    result = simulate_graph(
        graph,
        source_name="divider.pdf",
        sources=[{"net": "BUS28", "volts": 28}],
    )
    # No ground name -> falls back to most-connected... MID has 2 members.
    # Specify the return explicitly instead for a deterministic reference.
    graph["nets"][2]["name"] = "GND"
    result = simulate_graph(
        graph, source_name="divider.pdf", sources=[{"net": "BUS28", "volts": 28}]
    )
    assert result.ok, result.log[-400:]
    assert abs(result.nodeVoltages.get("bus28", 0) - 28.0) < 0.01
    assert abs(result.nodeVoltages.get("mid", 0) - 14.0) < 0.05  # equal coils divide


def test_symbol_map_uses_iec_style_defaults():
    """Exported symbols follow IEC 60617 style: KiCad's DEFAULT library symbols
    (Device:R is the IEC rectangle body), never the ANSI `_US` variants or the
    `*_IEEE` libraries."""
    from docling_serve.extractors.kicad_symbols import (
        TYPE_SYMBOL_MAP,
        SymbolLibrary,
        find_symbol_dir,
    )

    for _token, lib, name, _prefix in TYPE_SYMBOL_MAP:
        assert not lib.endswith("_IEEE"), f"{lib}:{name} is an IEEE-style library"
        assert not name.endswith("_US"), f"{lib}:{name} is the ANSI variant"

    symbol_dir = find_symbol_dir()
    if symbol_dir is None:
        pytest.skip("KiCad symbol libraries not installed")
    loaded = SymbolLibrary(symbol_dir).load("Device", "R")
    assert loaded is not None
    definition, _pins = loaded
    # IEC 60617 resistor = rectangle body; the ANSI zigzag has no rectangle.
    assert "(rectangle" in definition
