"""Local replacements for the deep-document framework helpers the schematic
extractor used. Keeps the schematic package self-contained on native docling-serve
(no deep_document bundle machinery)."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema


def write_json(path: Path, obj: Any) -> None:
    """Write a JSON artifact (pretty, UTF-8) — replaces deep_document.artifact_writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_schema(name: str) -> dict[str, Any]:
    # Schemas ship inside the schematic package (e.g. schematic-graph.schema.json).
    with resources.files("docling_serve.schematic").joinpath(name).open(
        "r", encoding="utf-8"
    ) as handle:
        return json.load(handle)


def validate_artifact(artifact: dict[str, Any], schema_name: str) -> None:
    """Validate an artifact against a packaged JSON schema (raises on failure)."""
    jsonschema.Draft202012Validator(_load_schema(schema_name)).validate(artifact)


def build_docling_structured(ctx: Any) -> dict[str, Any]:  # pragma: no cover
    """Stub: the native docling document is produced by docling itself now, so the
    schematic extractor's structural base is only used when a ConversionResult was
    supplied. The standalone /v1/schematic path passes conv_res=None and never
    reaches this; raise so any accidental use is loud rather than silently wrong."""
    raise RuntimeError(
        "build_docling_structured is not available on the native schematic path "
        "(conv_res is None); the schematic graph is the authoritative output."
    )
