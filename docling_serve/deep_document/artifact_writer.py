from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from docling_serve.deep_document.document_builder import build_deep_document
from docling_serve.deep_document.schema_validation import validate_artifact


@dataclass(frozen=True)
class DeepDocumentPaths:
    output_dir: Path
    deep_document: Path
    schemas_dir: Path


def write_deep_document_artifacts(
    manifest: dict[str, Any],
    output_dir: str | Path,
    *,
    source_manifest_key: str,
) -> DeepDocumentPaths:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    deep_document = build_deep_document(
        manifest=manifest,
        source_manifest_key=source_manifest_key,
    )
    validate_artifact(deep_document, "deep-document.schema.json")

    paths = DeepDocumentPaths(
        output_dir=target,
        deep_document=target / "deep-document.json",
        schemas_dir=target / "schemas",
    )
    write_json(paths.deep_document, deep_document)
    copy_schemas(paths.schemas_dir)
    return paths


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def copy_schemas(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for schema_file in resources.files("docling_serve.deep_document.schemas").iterdir():
        if schema_file.name.endswith(".schema.json"):
            shutil.copyfile(schema_file, target / schema_file.name)
