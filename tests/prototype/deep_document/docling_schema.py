"""JSON Schema for the prototype Docling-centric manifest.

The 3.0 manifest is a different shape from experiment5's 2.0 — its `units`
and `blocks` come from the Docling spine, not an OOXML walk. This is its
contract. Strict at the top level; lenient inside `blocks` because a block's
payload varies by `kind` (text/table/picture) and enrichment adds optional
fields.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BLOOM_LEVELS = ["remember", "understand", "apply", "analyze", "evaluate", "create"]
FORBIDDEN_KEYS = {"tldrawCommands", "slideImageRef"}


def _classification_schema() -> dict[str, Any]:
    return {
        "type": ["object", "null"],
        "properties": {
            "level": {"type": "string", "enum": BLOOM_LEVELS},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "provider": {"type": "string"},
            "method": {"type": "string"},
        },
        "required": ["level", "provider", "method"],
    }


MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://captify.local/schemas/deep-document-manifest-3.0.schema.json",
    "title": "Captify Deep Document Manifest (Docling-centric)",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schemaVersion", "artifactKind", "documentId", "documentType", "createdAt",
        "source", "extraction", "theme", "units", "assets", "diagnostics", "errors",
        "taxonomy", "coursewareAdvice", "usage",
    ],
    "properties": {
        "coursewareAdvice": {"type": "object"},
        "usage": {"type": "object"},
        "schemaVersion": {"const": "3.0"},
        "artifactKind": {"const": "deep_document_manifest"},
        "documentId": {"type": "string", "pattern": r"^[0-9a-f]{16}$"},
        "documentType": {"type": "string", "minLength": 1},
        "createdAt": {"type": "string"},
        "source": {
            "type": "object",
            "additionalProperties": True,
            "required": ["originalFileName", "sizeBytes", "sha256", "localPath"],
            "properties": {
                "originalFileName": {"type": "string"},
                "sizeBytes": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "localPath": {"type": "string"},
            },
        },
        "extraction": {
            "type": "object",
            "additionalProperties": True,
            "required": ["mode", "extractionId", "status"],
            "properties": {
                "mode": {"const": "deep"},
                "extractionId": {"type": "string", "pattern": r"^[0-9a-f]{16}$"},
                "status": {"type": "string", "enum": ["complete", "partial", "failed"]},
            },
        },
        "theme": {"type": ["object", "null"]},
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["unitId", "unitType", "index", "pageNumber", "blocks"],
                "properties": {
                    "unitId": {"type": "string", "pattern": r"^unit-\d{4}$"},
                    "unitType": {"type": "string"},
                    "index": {"type": "integer", "minimum": 0},
                    "pageNumber": {"type": "integer", "minimum": 1},
                    "title": {"type": ["string", "null"]},
                    "pageSizeEmu": {
                        "type": "object",
                        "required": ["cx", "cy"],
                        "properties": {
                            "cx": {"type": "integer", "minimum": 0},
                            "cy": {"type": "integer", "minimum": 0},
                        },
                    },
                    "blocks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "required": ["blockId", "kind", "bbox"],
                            "properties": {
                                "blockId": {"type": "string"},
                                "kind": {
                                    "type": "string",
                                    "enum": ["text", "table", "picture"],
                                },
                                "bbox": {
                                    "type": "object",
                                    "required": ["x", "y", "cx", "cy"],
                                },
                                "classification": _classification_schema(),
                            },
                        },
                    },
                    "classification": _classification_schema(),
                },
            },
        },
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["assetId", "kind"],
                "properties": {"assetId": {"type": "string", "pattern": r"^asset-"}},
            },
        },
        "diagnostics": {"type": "object"},
        "errors": {"type": "array"},
        "taxonomy": {"type": ["object", "null"]},
    },
}


def _walk_forbidden(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                errors.append(f"forbidden key at {path}.{key}")
            _walk_forbidden(child, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{index}]", errors)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Validate a 3.0 manifest. Returns a list of human-readable errors (empty = valid)."""
    errors: list[str] = []
    _walk_forbidden(manifest, "$", errors)
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        errors.append("jsonschema not installed; only forbidden-key sweep ran.")
        return errors
    validator = jsonschema.Draft202012Validator(MANIFEST_SCHEMA)
    for err in sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path)):
        location = "$" + "".join(
            f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path
        )
        errors.append(f"{location}: {err.message}")
    return errors


def write_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(MANIFEST_SCHEMA, indent=2, sort_keys=True) + "\n")
