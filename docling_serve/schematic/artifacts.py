"""JSON artifact writing and schema validation for schematic bundles."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_schema(name: str) -> dict[str, Any]:
    with (
        resources.files("docling_serve.schematic")
        .joinpath(name)
        .open("r", encoding="utf-8") as handle
    ):
        return json.load(handle)


def validate_artifact(artifact: dict[str, Any], schema_name: str) -> None:
    jsonschema.Draft202012Validator(_load_schema(schema_name)).validate(artifact)
