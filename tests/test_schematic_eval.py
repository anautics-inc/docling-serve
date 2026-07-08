"""Unit tests for the schematic eval scorer (pure, model-free)."""

from docling_serve.schematic.schematic_eval import (
    aggregate_runs,
    component_family,
    score_graph,
)


def test_component_family_normalizes_model_type_strings():
    assert component_family({"type": "Power Supply / transformer"}) == "power supply"
    assert component_family({"type": "Filter Capacitor"}) == "capacitor"
    assert component_family({"type": "Nixie Tube"}) == "tube"
    assert component_family({"type": "Microcontroller"}) == "ic"


def test_phantom_family_zeroes_component_score():
    label = {
        "drawing": "d",
        "componentsByFamily": {"resistor": 3},
        "forbiddenFamilies": {"capacitor": 0},
        "namedNets": [],
    }
    clean = {"components": [{"type": "resistor"}] * 3, "nets": []}
    dirty = {
        "components": [{"type": "resistor"}] * 3 + [{"type": "capacitor"}],
        "nets": [],
    }
    assert score_graph(clean, label).componentScore == 1.0
    dirty_score = score_graph(dirty, label)
    assert dirty_score.componentScore == 0.0
    assert dirty_score.phantomBreaches
    assert dirty_score.passed is False


def test_named_net_coverage_is_polarity_sensitive():
    label = {
        "drawing": "d",
        "componentsByFamily": {},
        "namedNets": ["+13 VDC", "-13 VDC"],
        "minNamedNetCoverage": 1.0,
    }
    # Only +13 present -> 50% coverage (polarity distinguishes the rails).
    graph = {"components": [], "nets": [{"name": "+13 vdc"}]}
    score = score_graph(graph, label)
    assert score.netNameCoverage == 0.5
    assert "-13 VDC" in score.missingNets


def test_named_net_matches_descriptor_variants_but_not_polarity():
    label = {"drawing": "d", "componentsByFamily": {}, "namedNets": ["+13 VDC OUTPUT"]}
    # "+13 VDC OUT" (subset of tokens) counts as present.
    ok = score_graph({"components": [], "nets": [{"name": "+13 VDC OUT"}]}, label)
    assert ok.netNameCoverage == 1.0
    # "-13 VDC OUTPUT" must NOT satisfy "+13 VDC OUTPUT".
    bad = score_graph({"components": [], "nets": [{"name": "-13 VDC OUTPUT"}]}, label)
    assert bad.netNameCoverage == 0.0


def test_family_tolerance_absorbs_ambiguous_counts():
    label = {
        "drawing": "d",
        "componentsByFamily": {"ground": 6},
        "componentTolerance": {"ground": 4},
        "namedNets": [],
    }
    # 3 grounds vs expected 6, tolerance 4 -> within tolerance, full score.
    graph = {"components": [{"type": "ground"}] * 3, "nets": []}
    assert score_graph(graph, label).componentScore == 1.0


def test_tuning_env_override(monkeypatch):
    from docling_serve.schematic.schematic_tuning import SchematicTuning

    monkeypatch.setenv("SCHEMATIC_DUPLICATE_MERGE_IOU", "0.7")
    monkeypatch.setenv("SCHEMATIC_MIN_VERIFIED_FRACTION_GATE", "0.8")
    tuning = SchematicTuning.from_env()
    assert tuning.duplicate_merge_iou == 0.7
    assert tuning.min_verified_fraction_gate == 0.8
    # Untouched field keeps its default.
    assert tuning.outline_min_box_pt == 90.0


def test_confidence_gate_flags_low_evidence():
    from docling_serve.schematic.connectivity_ids import record_connectivity_quality

    # Two components, neither with identity nor attachment -> 0% verified.
    graph = {
        "confidence": 0.9,
        "components": [{"id": "A", "type": "capacitor"}, {"id": "B", "type": "capacitor"}],
        "nets": [],
    }
    quality = record_connectivity_quality(graph)
    assert quality["verifiedComponentFraction"] == 0.0
    assert quality["needsReview"] is True
    # A fully-evidenced graph does not need review.
    graph2 = {
        "confidence": 0.9,
        "components": [{"id": "A", "type": "resistor", "value": "1k"}],
        "nets": [],
    }
    q2 = record_connectivity_quality(graph2)
    assert q2["needsReview"] is False


def test_aggregate_reports_variance():
    label = {"drawing": "d", "componentsByFamily": {"resistor": 2}, "namedNets": []}
    good = {"components": [{"type": "resistor"}] * 2, "nets": []}
    half = {"components": [{"type": "resistor"}], "nets": []}
    agg = aggregate_runs([score_graph(good, label), score_graph(half, label)])
    assert agg["runs"] == 2
    assert agg["overallSpread"] > 0
    assert 0 <= agg["passRate"] <= 1
