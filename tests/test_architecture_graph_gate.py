from __future__ import annotations

import json
from pathlib import Path

from scripts.check_architecture_graph import RATCHETS, architecture_metrics


def _node(identifier: str, source_file: str) -> dict[str, str]:
    return {"id": identifier, "source_file": source_file}


def _link(source: str, target: str) -> dict[str, str]:
    return {
        "source": source,
        "target": target,
        "relation": "imports",
        "confidence": "EXTRACTED",
    }


def test_architecture_metrics_detect_file_cycles_and_coupling() -> None:
    graph = {
        "nodes": [
            _node("settings", "docling_serve/settings.py"),
            _node("app", "docling_serve/app.py"),
            _node("adapter", "docling_serve/adapter.py"),
        ],
        "links": [
            _link("app", "settings"),
            _link("app", "adapter"),
            _link("adapter", "app"),
        ],
    }
    metrics = architecture_metrics(graph)
    assert metrics["largest_file_scc"] == 2
    assert metrics["settings_inbound_files"] == 1
    assert metrics["app_file_fan_out"] == 2
    assert metrics["max_file_fan_out"] == 2


def test_architecture_baseline_has_closed_ratchet_schema() -> None:
    baseline = json.loads(
        Path("architecture/graphify-baseline.json").read_text(encoding="utf-8")
    )
    assert baseline["graphify_version"] == "0.9.20"
    assert set(baseline["ratchet_ceilings"]) == set(RATCHETS)
