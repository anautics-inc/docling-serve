"""Round-2 expert-review fixes: wire-geometry survival, cross-pass frame
re-registration, connector value harvesting, and conservative pin adoption.
"""

from docling_serve.schematic.component_identity import (
    _apply_value,
    strip_connector_passive_values,
)
from docling_serve.schematic.connectivity_ids import normalize_quantity_values
from docling_serve.schematic.net_trace import TracedNet
from docling_serve.schematic.schematic_extractor import (
    _attach_traced_segments,
    _is_page_scale_box,
    _reregister_detection_frame,
    _traced_to_graph_nets,
)

PAGE_W, PAGE_H = 612.0, 792.0


# --- wire geometry survival --------------------------------------------------


def test_page_scale_box_is_excluded_from_clipping():
    # PS1 drawn as an enclosure around its own internal circuit: its bbox
    # covers most of the page and must not be used to clip wires away.
    assert _is_page_scale_box([86, 111, 496, 490], PAGE_W, PAGE_H)
    assert not _is_page_scale_box([86, 111, 150, 160], PAGE_W, PAGE_H)


def test_traced_segments_attach_to_model_nets_on_fallback():
    model_graph_nets = [
        {"id": "N1", "name": "26 VAC", "nodes": [{"component": "C0001"}, {"component": "C0002"}]},
        {"id": "N2", "name": "GND", "nodes": [{"component": "C0003"}]},
    ]
    traced = [
        TracedNet(
            components=["T1", "R1"],
            segments=[((10.0, 10.0), (50.0, 10.0)), ((50.0, 10.0), (50.0, 40.0))],
        ),
        TracedNet(components=["X9"], segments=[((1.0, 1.0), (2.0, 2.0))]),
    ]
    ref_to_id = {"T1": "C0001", "R1": "C0002"}
    attached = _attach_traced_segments(model_graph_nets, traced, 1, ref_to_id, {})
    assert attached == 1
    assert model_graph_nets[0]["segments"] == [
        [10.0, 10.0, 50.0, 10.0],
        [50.0, 10.0, 50.0, 40.0],
    ]
    assert model_graph_nets[0]["segmentsSource"] == "geometry-partial"
    assert "segments" not in model_graph_nets[1]


# --- cross-pass frame re-registration ----------------------------------------


def test_detection_frame_reregisters_onto_main_pass():
    # Main pass in the new frame; detection (cached from an older run) is in
    # a frame scaled by 0.5 — its boxes land at half the coordinates.
    main = [
        {"refDes": "R1", "bboxPt": [100.0, 100.0, 140.0, 120.0]},
        {"refDes": "R2", "bboxPt": [300.0, 200.0, 340.0, 220.0]},
        {"refDes": "R3", "bboxPt": [200.0, 400.0, 240.0, 420.0]},
    ]
    detection = {
        "components": [
            {"refDes": "R1", "bboxPt": [50.0, 50.0, 70.0, 60.0]},
            {"refDes": "R2", "bboxPt": [150.0, 100.0, 170.0, 110.0]},
            {"refDes": "R3", "bboxPt": [100.0, 200.0, 120.0, 210.0]},
            # Detected-only ground with no main-pass twin: must be carried
            # into the new frame by the same transform.
            {"refDes": None, "type": "ground", "bboxPt": [112.0, 67.5, 118.0, 72.5]},
        ]
    }
    assert _reregister_detection_frame(main, detection)
    ground = detection["components"][3]["bboxPt"]
    assert ground == [224.0, 135.0, 236.0, 145.0]


def test_detection_frame_left_alone_when_aligned():
    main = [
        {"refDes": "R1", "bboxPt": [100.0, 100.0, 140.0, 120.0]},
        {"refDes": "R2", "bboxPt": [300.0, 200.0, 340.0, 220.0]},
        {"refDes": "R3", "bboxPt": [200.0, 400.0, 240.0, 420.0]},
    ]
    detection = {
        "components": [
            {"refDes": "R1", "bboxPt": [101.0, 99.0, 141.0, 121.0]},
            {"refDes": "R2", "bboxPt": [299.0, 201.0, 339.0, 219.0]},
            {"refDes": "R3", "bboxPt": [201.0, 399.0, 241.0, 421.0]},
        ]
    }
    before = [list(c["bboxPt"]) for c in detection["components"]]
    assert not _reregister_detection_frame(main, detection)
    assert [list(c["bboxPt"]) for c in detection["components"]] == before


# --- connector value harvesting ----------------------------------------------


def test_crop_value_rejected_on_connector():
    connector = {"id": "C7", "type": "Connector", "refDes": "J1"}
    assert not _apply_value(connector, {"value": "1K"})
    assert connector.get("value") is None
    # A resistor with the same reading keeps it.
    resistor = {"id": "C8", "type": "Resistor", "refDes": "R1"}
    assert _apply_value(resistor, {"value": "1K"})


def test_strip_connector_passive_values():
    graph = {
        "components": [
            {"id": "C1", "type": "Connector", "refDes": "J1", "value": "1K"},
            {"id": "C2", "type": "Resistor", "refDes": "R1", "value": "1K"},
            {"id": "C3", "type": "off-page", "refDes": "SYNCHRO EXC", "value": "2K"},
            {"id": "C4", "type": "Connector", "refDes": "P1", "value": "MS3106A"},
        ]
    }
    assert strip_connector_passive_values(graph) == 2
    assert graph["components"][0]["value"] is None
    assert graph["components"][0]["value_was"] == "1K"
    assert graph["components"][1]["value"] == "1K"  # the real resistor keeps it
    assert graph["components"][2]["value"] is None
    assert graph["components"][3]["value"] == "MS3106A"  # part-shaped, kept


def test_quantity_annotation_moves_to_quantity_attribute():
    graph = {
        "components": [
            {"id": "C1", "type": "ground", "value": "2"},
            {"id": "C2", "type": "resistor", "value": "(3)"},
            {"id": "C3", "type": "resistor", "value": "2"},  # a real 2-ohm value
        ]
    }
    assert normalize_quantity_values(graph) == 2
    assert graph["components"][0]["quantity"] == 2
    assert graph["components"][0]["value"] is None
    assert graph["components"][1]["quantity"] == 3
    assert graph["components"][2]["value"] == "2"


# --- conservative pin adoption -----------------------------------------------


def _transformer_case():
    """T1's winding side (3 attachments) and output side (1 attachment) both
    overlap the same model net that claims pins 14/15/3 on T1."""
    model_nets = [
        {
            "name": "26 VAC",
            "nodes": [
                {"refDes": "T1", "pin": "14"},
                {"refDes": "T1", "pin": "15"},
                {"refDes": "T1", "pin": "3"},
                {"refDes": "R1"},
            ],
        }
    ]
    traced = [
        # Output side: T1 touches once. Listed first to prove score ordering
        # (not list order) drives pin consumption.
        TracedNet(
            components=["R1", "T1"],
            segments=[((0.0, 0.0), (5.0, 0.0))],
            attachments={"T1": [(60.0, 10.0)], "R1": [(40.0, 10.0)]},
        ),
        # Winding side: T1 attaches three times.
        TracedNet(
            components=["R1", "T1"],
            segments=[((0.0, 0.0), (1.0, 0.0))],
            attachments={
                "T1": [(10.0, 10.0), (10.0, 20.0), (10.0, 30.0)],
                "R1": [(30.0, 10.0)],
            },
        ),
    ]
    return model_nets, traced


def test_pin_claims_require_exact_attachment_count():
    model_nets, traced = _transformer_case()
    ref_to_id = {"T1": "C0001", "R1": "C0002"}
    nets = _traced_to_graph_nets(traced, model_nets, 1, ref_to_id, {})
    output_side, winding_side = nets[0], nets[1]

    winding_pins = sorted(
        n["pin"] for n in winding_side["nodes"] if n["component"] == "C0001" and n["pin"]
    )
    assert winding_pins == ["14", "15", "3"]

    # The single output-side attachment must NOT inherit one of the winding
    # pins (the reviewed pin-15 misassignment): counts don't line up and the
    # pins are already consumed by the better-matched winding net.
    output_pins = [
        n["pin"] for n in output_side["nodes"] if n["component"] == "C0001"
    ]
    assert output_pins == [None]
