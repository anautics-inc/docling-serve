"""Extraction-quality hardening: evidence-backed reattachment, off-page
connectivity semantics, island-joining KiCad labels, and the generated-doc
ERC policy project file.
"""

from pathlib import Path

from docling_serve.schematic.connectivity_ids import (
    drop_quantity_annotations,
    drop_value_text_echoes,
    merge_duplicate_detections,
    reattach_floating_components,
)
from docling_serve.schematic.kicad_sch import (
    net_label_sexprs,
    project_file_json,
    write_project_files,
)
from docling_serve.schematic.schematic_revision import check_graph_integrity


def test_reattach_by_description_prefers_most_specific_net():
    graph = {
        "components": [
            {
                "id": "C5",
                "type": "Capacitor",
                "description": "Filter capacitor on +13 VDC output",
            },
        ],
        "nets": [
            {"id": "N1", "name": "+13 VDC", "nodes": [{"component": "R1"}]},
            {"id": "N2", "name": "+13 VDC OUTPUT", "nodes": [{"component": "R1"}]},
        ],
    }
    notes = reattach_floating_components(graph)
    assert notes == ["reattached C5 -> +13 VDC OUTPUT (description-inference)"]
    members = [n.get("component") for n in graph["nets"][1]["nodes"]]
    assert "C5" in members
    node = graph["nets"][1]["nodes"][-1]
    assert node["membershipSource"] == "description-inference"


def test_reattach_by_refdes_alias_tokens():
    # "SIG GND" net matches component text via the sig->signal, gnd->ground aliases.
    graph = {
        "components": [
            {"id": "C6", "type": "Capacitor", "description": "Filter capacitor on signal ground"},
        ],
        "nets": [
            {"id": "N1", "name": "SIG GND", "nodes": [{"component": "X1"}]},
            {"id": "N2", "name": "B+", "nodes": [{"component": "X1"}]},
        ],
    }
    notes = reattach_floating_components(graph)
    assert len(notes) == 1 and "SIG GND" in notes[0]


def test_reattach_by_segment_terminus():
    graph = {
        "components": [
            {"id": "C7", "type": "capacitor", "bbox": [100, 100, 120, 110], "page": 1},
        ],
        "nets": [
            {
                "id": "N1",
                "name": None,
                "page": 1,
                "nodes": [{"component": "X1"}],
                "segments": [[50, 105, 101, 105]],
            },
            {
                "id": "N2",
                "name": None,
                "page": 1,
                "nodes": [{"component": "X1"}],
                "segments": [[300, 300, 400, 300]],
            },
        ],
    }
    notes = reattach_floating_components(graph)
    assert len(notes) == 1 and "segment-terminus" in notes[0]
    node = graph["nets"][0]["nodes"][-1]
    assert node["component"] == "C7"
    assert node["attachment"] == [101, 105]


def test_reattach_skips_ambiguous_evidence():
    graph = {
        "components": [
            {"id": "C8", "type": "capacitor", "description": "Filter capacitor on B+ and B-"},
        ],
        "nets": [
            {"id": "N1", "name": "B+", "nodes": []},
            {"id": "N2", "name": "B-", "nodes": []},
        ],
    }
    assert reattach_floating_components(graph) == []


def test_connector_named_after_wire_attaches_by_subset():
    graph = {
        "components": [
            {"id": "C34", "type": "connector", "refDes": "SYNCHRO EXC"},
        ],
        "nets": [
            {"id": "N1", "name": "26 VAC TO SYNCHRO EXC", "nodes": [{"component": "R3"}]},
            {"id": "N2", "name": "26 VAC", "nodes": [{"component": "R3"}]},
        ],
    }
    notes = reattach_floating_components(graph)
    assert notes == ["reattached SYNCHRO EXC -> 26 VAC TO SYNCHRO EXC (description-inference)"]


def test_connector_subset_rule_requires_unique_match():
    graph = {
        "components": [{"id": "C1", "type": "connector", "refDes": "VAC"}],
        "nets": [
            {"id": "N1", "name": "26 VAC A", "nodes": []},
            {"id": "N2", "name": "26 VAC B", "nodes": []},
        ],
    }
    assert reattach_floating_components(graph) == []


def test_value_text_echoes_dropped():
    graph = {
        "components": [
            {"id": "R1", "refDes": "R1", "type": "Resistor", "value": "1K"},
            {"id": "E1", "refDes": "1K", "type": "resistor", "value": None},
            # Same refDes pattern but WITH its own value: kept (a real part).
            {"id": "E2", "refDes": "2K", "type": "resistor", "value": "2K"},
        ],
        "nets": [{"id": "N1", "nodes": [{"component": "R1"}]}],
    }
    notes = drop_value_text_echoes(graph)
    assert len(notes) == 1 and "printed value of R1" in notes[0]
    ids = [c["id"] for c in graph["components"]]
    assert ids == ["R1", "E2"]


def test_glyph_check_removes_confident_phantom_capacitor():
    from docling_serve.schematic.component_identity import disambiguate_capacitor_glyphs

    graph = {
        "pages": [{"pageNumber": 1, "width": 612, "height": 792}],
        "components": [
            {"id": "C1", "type": "capacitor", "page": 1, "bbox": [100, 100, 110, 112]},
        ],
        "nets": [
            {
                "id": "N1",
                "page": 1,
                "nodes": [
                    {"component": "C1", "membershipSource": "description-inference"}
                ],
            }
        ],
    }
    # A fake understand() that returns a confident "not a capacitor" verdict,
    # and a fake page image so a crop is attempted.
    def fake_understand(prompt, system, png):
        return {"kind": "other", "confidence": 0.95, "reason": "just a wire with bars"}

    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (612, 792), "white").save(buf, format="PNG")
    notes = disambiguate_capacitor_glyphs(
        graph, [(1, buf.getvalue())], understand=fake_understand
    )
    assert any("removed" in n for n in notes)
    assert graph["components"] == []
    assert graph["nets"][0]["nodes"] == []


def test_quantity_annotations_dropped_with_memberships():
    graph = {
        "components": [
            {"id": "Q1", "refDes": "(2)", "type": "capacitor", "value": "2"},
            {"id": "R1", "refDes": "R1", "type": "resistor", "value": "1K"},
        ],
        "nets": [{"id": "N1", "nodes": [{"component": "Q1"}, {"component": "R1"}]}],
    }
    notes = drop_quantity_annotations(graph)
    assert len(notes) == 1 and "quantity annotation" in notes[0]
    assert [c["id"] for c in graph["components"]] == ["R1"]
    assert [n["component"] for n in graph["nets"][0]["nodes"]] == ["R1"]


def test_duplicate_detections_merge_by_overlap_not_containment():
    graph = {
        "components": [
            # The model's component (rich), overlapping D1 heavily.
            {
                "id": "C5",
                "type": "Capacitor",
                "description": "Filter capacitor on +13 VDC output",
                "bbox": [190, 125, 210, 145],
            },
            # Same glyph, ~64% IoU with C5 -> merges.
            {
                "id": "D1",
                "type": "capacitor",
                "description": "capacitor (detected #1)",
                "bbox": [192, 127, 212, 147],
            },
            # A large enclosure box that GEOMETRICALLY CONTAINS a small echo
            # but shares almost no area with it -> must NOT swallow it.
            {"id": "PS1", "type": "Power Supply", "bbox": [100, 100, 400, 400]},
            {
                "id": "D2",
                "type": "ground",
                "description": "ground (detected #2)",
                "bbox": [150, 150, 160, 160],
            },
        ],
        "nets": [
            {"id": "N1", "nodes": [{"component": "D1", "attachment": [200, 135]}]},
        ],
    }
    notes = merge_duplicate_detections(graph)
    assert len(notes) == 1 and "into C5" in notes[0]
    ids = [c["id"] for c in graph["components"]]
    assert "D1" not in ids  # merged
    assert "D2" in ids  # NOT swallowed by the enclosing PS1 box
    assert graph["nets"][0]["nodes"][0]["component"] == "C5"


def test_connectivity_named_single_ended_is_off_page_pass():
    graph = {
        "schemaVersion": "captify.schematic.v1",
        "components": [{"id": "R1", "bbox": [0, 0, 1, 1]}],
        "nets": [
            {"id": "N1", "name": "B+", "nodes": [{"component": "R1"}], "segments": [[0, 0, 1, 1]]},
        ],
    }
    checks = {c.id: c for c in check_graph_integrity(graph)}
    assert checks["connectivity"].status == "pass"
    assert "off-page" in checks["connectivity"].detail


def test_connectivity_unnamed_single_ended_warns():
    graph = {
        "schemaVersion": "captify.schematic.v1",
        "components": [{"id": "R1", "bbox": [0, 0, 1, 1]}],
        "nets": [
            {"id": "N1", "name": None, "nodes": [{"component": "R1"}], "segments": [[0, 0, 1, 1]]},
        ],
    }
    checks = {c.id: c for c in check_graph_integrity(graph)}
    assert checks["connectivity"].status == "warn"


def test_labels_terminate_dangling_copper_ends():
    nets = [
        {
            # An L-shaped run: two dangling ends, one shared corner.
            "id": "N1",
            "name": "+13 VDC",
            "page": 1,
            "segments": [[0, 0, 100, 0], [100, 0, 100, 50]],
            "nodes": [],
        },
        {
            # A closed loop: no dangling ends -> one on-wire identity label.
            "id": "N2",
            "name": "LOOP",
            "page": 1,
            "segments": [[0, 100, 50, 100], [50, 100, 0, 100]],
            "nodes": [],
        },
        {
            # Model-sourced net without copper: labeled at stub emission
            # (build_symbol_instances), never here.
            "id": "N3",
            "name": None,
            "wireId": "W007",
            "page": 1,
            "segments": [],
            "nodes": [{"component": "C1", "attachment": [10, 20]}],
        },
    ]
    items = net_label_sexprs(nets, page_no=1)
    global_ = [i for i in items if i.lstrip().startswith("(global_label")]
    # Dangling ends get GLOBAL terminators; closed copper gets one global
    # identity label (all-global — a local twin would trip
    # same_local_global_label).
    assert sum('"+13 VDC"' in g for g in global_) == 2
    assert sum('"LOOP"' in g for g in global_) == 1
    # No floating labels for copper-less nets.
    assert not any("W007" in i for i in items)


def test_project_file_ignores_off_grid(tmp_path: Path):
    assert '"endpoint_off_grid": "ignore"' in project_file_json()
    sch = tmp_path / "schematic.kicad_sch"
    sch.write_text("(kicad_sch)")
    written = write_project_files([sch])
    assert written == [tmp_path / "schematic.kicad_pro"]
    assert written[0].exists()
