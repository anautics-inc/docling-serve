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
