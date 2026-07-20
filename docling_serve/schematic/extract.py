"""Schematic (engineering drawing) extraction entry point.

A genuine docling gap: a wiring diagram's meaning is its geometry — component
symbols and the nets (wires) connecting their pins — which docling's text/layout
conversion does not recover. This runs the vector + (optional) vision pipeline to
produce a ``captify.schematic.v1`` component/net graph plus derived artifacts
(SVG, KiCad schematic, netlist, EDML, EEvision, XML).

Framework-free: it constructs a lightweight ExtractionContext (conv_res=None — the
base document is docling's native job) and runs the SchematicExtractor into a
working directory, then optionally publishes the artifacts to S3.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from docling_serve.artifacts.publish import publish_dir_to_s3
from docling_serve.schematic.base import ExtractionContext
from docling_serve.schematic.schematic_extractor import (
    SCHEMATIC_PROFILES,
    SCHEMATIC_SUFFIXES,
    SchematicExtractor,
    _looks_like_vector_drawing,
)

_log = logging.getLogger(__name__)


def is_schematic_candidate(path: Path, profile: str = "auto") -> bool:
    """True when ``path`` should run the schematic extractor for ``profile``."""
    if path.suffix.lower() not in SCHEMATIC_SUFFIXES:
        return False
    p = (profile or "default").strip().lower()
    if p in SCHEMATIC_PROFILES:
        return True
    if p == "auto" and path.suffix.lower() == ".pdf":
        return _looks_like_vector_drawing(path)
    return False


def extract_schematic(
    pdf_path: Path,
    output_dir: Path,
    *,
    profile: str = "schematic",
    tenant_id: str | None = None,
    source_key: str | None = None,
    progress: Any | None = None,
) -> dict[str, Any]:
    """Run the schematic extractor; write artifacts under ``output_dir``.

    Returns ``{structured, artifacts, manifest, graph, domain, notes}`` where
    ``artifacts`` are bundle-relative paths written under ``output_dir`` and
    ``graph`` is the parsed ``schematic/schematic-graph.json`` payload.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_key = source_key or f"task:{tenant_id or 'default'}:{pdf_path.stem}"
    ctx = ExtractionContext(
        source_path=pdf_path,
        bundle_dir=output_dir,
        media_dir=output_dir / "media",
        source_manifest_key=manifest_key,
        task_id=pdf_path.stem,
        profile=profile,
        conv_res=None,
        source_dir=pdf_path.parent,
        progress=progress,
    )
    result = SchematicExtractor().build(ctx)

    import json

    # Minimal bundle manifest at the root so the schematic check/revise operations
    # (which read {prefix}/extraction.json and require domain == "schematic") can
    # locate the published bundle — the native flow has no deep-document manifest.
    manifest = {"domain": result.domain or "schematic", **result.manifest_extra}
    (output_dir / "extraction.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    graph: dict[str, Any] | None = None
    graph_path = output_dir / "schematic" / "schematic-graph.json"
    if graph_path.is_file():
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            graph = None

    return {
        "structured": result.structured,
        "artifacts": result.artifacts,
        "manifest": result.manifest_extra,
        "graph": graph,
        "domain": result.domain,
        "notes": result.notes,
    }


__all__ = ["extract_schematic", "is_schematic_candidate", "publish_dir_to_s3"]
