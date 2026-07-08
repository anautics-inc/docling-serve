"""SPICE emission usability: the generated model must be solvable by default.

Covers the extraction-QA hardening: polarity-preserving node names, ground
nets collapsing to node 0, floating components omitted, type-aware assumed
values, and the auto-sourced runnable deck shared by the delivery-check gate
and the /simulate action.
"""

from docling_serve.schematic.spice import (
    graph_to_spice,
    ground_net_ids,
    net_node_names,
    sanitize_node,
)
from docling_serve.schematic.spice_simulation import (
    detect_power_nets,
    runnable_deck,
)


def _graph(components, nets):
    return {
        "schemaVersion": "captify.schematic.v1",
        "components": components,
        "nets": nets,
    }


def test_sanitize_node_preserves_polarity():
    assert sanitize_node("+13 VDC") == "P13_VDC"
    assert sanitize_node("-13 VDC") == "N13_VDC"
    assert sanitize_node("B+") == "B_P"
    assert sanitize_node("B-") == "B_N"
    assert sanitize_node("A8B22") == "A8B22"
    assert sanitize_node("") == "X"


def test_polarity_rails_never_collide():
    graph = _graph(
        [],
        [
            {"id": "N1", "name": "+13 VDC", "nodes": []},
            {"id": "N2", "name": "-13 VDC", "nodes": []},
        ],
    )
    names = net_node_names(graph)
    assert names["N1"] == "P13_VDC"
    assert names["N2"] == "N13_VDC"


def test_ground_nets_collapse_to_node_zero():
    graph = _graph(
        [
            {"id": "G1", "type": "ground", "refDes": None},
            {"id": "R1", "type": "resistor", "refDes": "R1", "value": "1k"},
        ],
        [
            # Ground by NAME.
            {"id": "N1", "name": "CHASSIS GROUND", "nodes": [{"component": "R1"}]},
            # Ground because a ground SYMBOL touches it.
            {
                "id": "N2",
                "name": "W009",
                "nodes": [{"component": "G1"}, {"component": "R1"}],
            },
            # Plain signal net.
            {"id": "N3", "name": "SIG A", "nodes": [{"component": "R1"}]},
        ],
    )
    assert ground_net_ids(graph) == {"N1", "N2"}
    names = net_node_names(graph)
    assert names["N1"] == "0"
    assert names["N2"] == "0"
    assert names["N3"] == "SIG_A"


def test_ground_symbols_not_emitted_as_components():
    graph = _graph(
        [
            {"id": "G1", "type": "ground", "refDes": "GND1"},
            {"id": "R1", "type": "resistor", "refDes": "R1", "value": "1k"},
        ],
        [
            {
                "id": "N1",
                "name": "RET",
                "nodes": [{"component": "G1"}, {"component": "R1"}],
            },
            {"id": "N2", "name": "SIG", "nodes": [{"component": "R1"}]},
        ],
    )
    netlist = graph_to_spice(graph, source_name="t.pdf")
    assert "SC_GROUND" not in netlist
    assert "XGND1" not in netlist
    assert "RR1 0 SIG 1k" in netlist


def test_floating_components_omitted_with_audit_comment():
    graph = _graph(
        [
            {"id": "C9", "type": "capacitor", "refDes": "C9"},
            {"id": "R1", "type": "resistor", "refDes": "R1", "value": "2k"},
        ],
        [
            {
                "id": "N1",
                "name": "A",
                "nodes": [{"component": "R1"}, {"component": "R1"}],
            }
        ],
    )
    netlist = graph_to_spice(graph, source_name="t.pdf")
    assert "* OMITTED C9: no traced net membership" in netlist
    assert "NC_C9" not in netlist


def test_unlabeled_passives_get_type_aware_assumed_values():
    graph = _graph(
        [{"id": "C1", "type": "capacitor", "refDes": "C1"}],
        [
            {"id": "N1", "name": "A", "nodes": [{"component": "C1"}]},
            {"id": "N2", "name": "B", "nodes": [{"component": "C1"}]},
        ],
    )
    netlist = graph_to_spice(graph, source_name="t.pdf")
    assert "CC1 A B 100n" in netlist
    assert "* ASSUMED CC1 value 100n" in netlist


def test_detect_power_nets_signed_volts_and_ac():
    supplies, grounds = detect_power_nets(
        _graph(
            [],
            [
                {"id": "N1", "name": "+13 VDC", "nodes": []},
                {"id": "N2", "name": "-13 VDC", "nodes": []},
                {"id": "N3", "name": "26 VAC", "nodes": []},
                {"id": "N4", "name": "SIG GND", "nodes": []},
                {"id": "N5", "name": "RTN", "nodes": []},
            ],
        )
    )
    by_net = {s["net"]: s for s in supplies}
    assert by_net["N1"]["volts"] == 13.0
    assert by_net["N2"]["volts"] == -13.0
    assert by_net["N3"]["volts"] == 26.0
    assert by_net["N3"].get("ac") is True
    assert set(grounds) == {"N4", "N5"}


def test_runnable_deck_sources_land_on_emitted_nodes():
    graph = _graph(
        [
            {"id": "R1", "type": "resistor", "refDes": "R1", "value": "1k"},
            {"id": "G1", "type": "ground", "refDes": None},
        ],
        [
            {"id": "N1", "name": "+13 VDC", "nodes": [{"component": "R1"}]},
            {"id": "N2", "name": "-13 VDC", "nodes": [{"component": "R1"}]},
            {
                "id": "N3",
                "name": "RET",
                "nodes": [{"component": "G1"}, {"component": "R1"}],
            },
        ],
    )
    netlist = graph_to_spice(graph, source_name="t.pdf")
    deck, info = runnable_deck(netlist, graph)
    assert "V_SUP1 P13_VDC 0 DC 13.0" in deck
    assert "V_SUP2 N13_VDC 0 DC -13.0" in deck
    assert ".option rshunt=1e9" in deck
    assert deck.rstrip().endswith(".end")
    assert ".op" in deck
    # The RET net is already node 0 via the ground symbol — no tie needed.
    assert "V_GNDREF" not in deck
    assert [s["volts"] for s in info["supplies"]] == [13.0, -13.0]


def test_check_report_dict_shape():
    from docling_serve.schematic.schematic_revision import (
        CheckResult,
        check_report_dict,
    )

    report = check_report_dict(
        [
            CheckResult("schema", "Graph schema", "pass", "ok"),
            CheckResult("spice", "SPICE", "warn", "hm"),
        ]
    )
    assert report["passed"] is True
    assert len(report["checks"]) == 2
    assert report["checkedAt"]

    failing = check_report_dict([CheckResult("spice", "SPICE", "fail", "no")])
    assert failing["passed"] is False
