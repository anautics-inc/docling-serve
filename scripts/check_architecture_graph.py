"""Validate production coupling against a reviewed Graphify baseline."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scripts.architecture_evidence import (
    CORE_PREFIX,
    DEPENDENCY_RELATIONS,
    GRAPHIFY_VERSION,
    SCHEMA_VERSION,
    SNAPSHOT_ALGORITHM,
    source_snapshot,
)

RATCHETS = (
    "largest_file_scc",
    "settings_inbound_files",
    "app_file_fan_out",
    "max_file_fan_out",
    "dangling_edge_ratio",
    "upload_staging_symbols",
    "legacy_office_symbols",
    "schematic_extractor_symbols",
)


def _largest_scc(adjacency: dict[str, set[str]]) -> int:
    index = 0
    stack: list[str] = []
    active: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    largest = 0

    def visit(node: str) -> None:
        nonlocal index, largest
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for target in adjacency.get(node, set()):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in active:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        size = 0
        while stack:
            member = stack.pop()
            active.remove(member)
            size += 1
            if member == node:
                break
        largest = max(largest, size)

    nodes = set(adjacency)
    nodes.update(target for targets in adjacency.values() for target in targets)
    for node in nodes:
        if node not in indices:
            visit(node)
    return largest


def architecture_metrics(graph: dict[str, Any]) -> dict[str, int | float]:
    nodes = {
        str(node["id"]): node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("id")
    }
    links = [link for link in graph.get("links", []) if isinstance(link, dict)]
    inbound: dict[str, set[str]] = defaultdict(set)
    outbound: dict[str, set[str]] = defaultdict(set)
    adjacency: dict[str, set[str]] = defaultdict(set)
    symbols: Counter[str] = Counter()
    for node in nodes.values():
        source_file = node.get("source_file")
        if isinstance(source_file, str) and source_file.startswith(CORE_PREFIX):
            symbols[source_file] += 1
    dangling = 0
    production_dependency_links = 0
    for link in links:
        source = nodes.get(str(link.get("source")))
        target = nodes.get(str(link.get("target")))
        if link.get("relation") not in DEPENDENCY_RELATIONS:
            continue
        if str(link.get("confidence", "")).upper() == "INFERRED":
            continue
        source_file = source.get("source_file") if source is not None else None
        if not (isinstance(source_file, str) and source_file.startswith(CORE_PREFIX)):
            continue
        production_dependency_links += 1
        if target is None:
            dangling += 1
            continue
        target_file = target.get("source_file")
        if not (
            isinstance(source_file, str)
            and isinstance(target_file, str)
            and source_file.startswith(CORE_PREFIX)
            and target_file.startswith(CORE_PREFIX)
            and source_file != target_file
        ):
            continue
        inbound[target_file].add(source_file)
        outbound[source_file].add(target_file)
        if link.get("relation") in {"imports", "imports_from"}:
            adjacency[source_file].add(target_file)
    return {
        "nodes": len(nodes),
        "edges": len(links),
        "largest_file_scc": _largest_scc(adjacency),
        "settings_inbound_files": len(inbound[f"{CORE_PREFIX}settings.py"]),
        "app_file_fan_out": len(outbound[f"{CORE_PREFIX}app.py"]),
        "max_file_fan_out": max(map(len, outbound.values()), default=0),
        "dangling_edge_ratio": (
            round(dangling / production_dependency_links, 4)
            if production_dependency_links
            else 0.0
        ),
        "upload_staging_symbols": symbols[f"{CORE_PREFIX}upload_staging.py"],
        "legacy_office_symbols": symbols[f"{CORE_PREFIX}legacy_office.py"],
        "schematic_extractor_symbols": symbols[
            f"{CORE_PREFIX}schematic/schematic_extractor.py"
        ],
    }


def baseline_failures(
    baseline: dict[str, Any],
    metrics: dict[str, int | float],
    provenance: dict[str, Any],
    repository: Path,
) -> list[str]:
    failures: list[str] = []
    if baseline.get("schema_version") != SCHEMA_VERSION:
        failures.append("architecture baseline schema version is stale")
    if baseline.get("graphify_version") != GRAPHIFY_VERSION:
        failures.append("architecture baseline Graphify version is stale")
    if provenance.get("algorithm") != SNAPSHOT_ALGORITHM:
        failures.append("scanned provenance algorithm is unsupported")
    current = source_snapshot(repository)
    if any(provenance.get(key) != current.get(key) for key in ("digest", "file_count")):
        failures.append("Graphify evidence does not match the current working tree")
    ceilings = baseline.get("ratchet_ceilings")
    if not isinstance(ceilings, dict) or set(ceilings) != set(RATCHETS):
        failures.append("architecture ratchet ceiling schema is invalid")
        return failures
    for name in RATCHETS:
        ceiling = ceilings[name]
        if (
            isinstance(ceiling, bool)
            or not isinstance(ceiling, (int, float))
            or not math.isfinite(ceiling)
            or ceiling < 0
        ):
            failures.append(f"{name} ceiling is invalid")
        elif metrics[name] > ceiling:
            failures.append(f"{name}={metrics[name]} exceeds ceiling {ceiling}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, default=Path("graphify-out/graph.json"))
    parser.add_argument(
        "--provenance",
        type=Path,
        default=Path("graphify-out/source-snapshot.json"),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("architecture/graphify-baseline.json"),
    )
    args = parser.parse_args()
    graph = json.loads(args.graph.read_text())
    provenance = json.loads(args.provenance.read_text())
    baseline = json.loads(args.baseline.read_text())
    metrics = architecture_metrics(graph)
    failures = baseline_failures(baseline, metrics, provenance, Path.cwd())
    if failures:
        raise SystemExit("\n".join(failures))
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
