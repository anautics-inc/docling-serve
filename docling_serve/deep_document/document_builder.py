from __future__ import annotations

import json
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_deep_document(
    *,
    manifest: dict[str, Any],
    source_manifest_key: str,
) -> dict[str, Any]:
    """Build the unified ``deep-document.json`` object from a normalized manifest.

    Pure structural extraction — units, elements, geometry, images, canvas. No
    course-model or pedagogical fields.
    """
    units = normalized_units(manifest)
    return {
        "schemaVersion": "1.0",
        "artifactKind": "deep_document",
        "documentId": manifest.get("documentId") or document_id(manifest),
        "sourceManifestKey": source_manifest_key,
        "createdAt": datetime.now(UTC).isoformat(),
        "source": manifest.get("source") or {},
        "storage": {
            "layout": "relative_object_tree",
            "manifestPath": "deep-document.json",
        },
        "document": {
            "title": source_title(manifest),
            "unitCount": len(units),
            "unitType": common_unit_type(units),
            "units": units,
        },
        "assets": manifest.get("assets") or [],
        "canvas": build_canvas_contract(units),
        "rawArtifacts": raw_artifacts(manifest),
        "provenance": {"generator": "docling_serve.deep_document.document_builder"},
        "errors": manifest.get("errors") or [],
    }


def normalized_units(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    source_units = manifest.get("units")
    if not isinstance(source_units, list):
        source_units = manifest.get("slides") or []
    units = []
    for index, unit in enumerate(source_units):
        if not isinstance(unit, dict):
            continue
        unit_id = str(
            unit.get("unitId") or unit.get("slideId") or f"unit-{index + 1:04d}"
        )
        unit_type = str(
            unit.get("unitType") or ("slide" if unit.get("slideId") else "unit")
        )
        unit_number = int(unit.get("slideNumber") or unit.get("index") or index) + (
            1 if unit.get("slideNumber") is None else 0
        )
        elements = normalized_elements(unit)
        render = unit.get("render") or {}
        source_refs = unit.get("sourceRefs") or {}
        title_lines = (
            ((unit.get("slideFormat") or {}).get("titleStructure") or {}).get("lines")
        ) or []
        header = str(title_lines[0]).strip() if title_lines else ""
        sub_header = " - ".join(
            str(line).strip() for line in title_lines[1:] if str(line).strip()
        )
        units.append(
            {
                "unitId": unit_id,
                "unitType": unit_type,
                "unitNumber": unit_number,
                "title": unit.get("title") or header or f"Unit {unit_number}",
                "sourceRefs": {
                    **source_refs,
                    "sourcePart": source_refs.get("sourcePart")
                    or unit.get("sourcePart"),
                    "sourceManifestKey": source_refs.get("sourceManifestKey")
                    or unit.get("sourceManifestKey"),
                },
                "render": {
                    "size": render.get("size") or unit.get("size") or {},
                    "background": render.get("background")
                    or unit.get("background")
                    or {},
                },
                "content": {
                    "header": header,
                    "subHeader": sub_header,
                    "plainText": plain_text(elements),
                    "speakerNotes": unit.get("speakerNotes")
                    or {"raw": "", "cleaned": ""},
                    "tables": [
                        element
                        for element in elements
                        if element.get("type") == "table"
                    ],
                    "images": [
                        element
                        for element in elements
                        if element.get("type") == "image"
                    ],
                    "elements": elements,
                },
                "canvas": {
                    "frameId": f"frame-{unit_id}",
                    "shapeIds": [
                        f"shape-{element.get('elementId')}" for element in elements
                    ],
                },
            }
        )
    return units


def normalized_elements(unit: dict[str, Any]) -> list[dict[str, Any]]:
    elements = []
    for index, element in enumerate(unit.get("elements") or [], start=1):
        if not isinstance(element, dict):
            continue
        element_id = str(
            element.get("elementId")
            or element.get("id")
            or f"{unit.get('unitId', 'unit')}-element-{index:04d}"
        )
        elements.append(
            {
                "elementId": element_id,
                "type": element.get("type") or "unknown",
                "kind": element.get("kind"),
                "bbox": element.get("bbox") or element.get("bounds") or {},
                "zIndex": element.get("zIndex"),
                "text": element.get("text") or {},
                "style": element.get("style") or {},
                "assetRef": element.get("assetRef") or element.get("assetId"),
                "sourceRefs": element.get("sourceRefs") or element.get("source") or {},
                "quality": element.get("quality") or {},
            }
        )
    return elements


def build_canvas_contract(units: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "provider": "tldraw",
        "coordinateSystem": {"unit": "px", "origin": "top-left"},
        "documentFrame": {"layout": "vertical", "unitSpacing": 120},
        "shapeMap": {
            unit["unitId"]: {
                "frameId": unit["canvas"]["frameId"],
                "sourceImageShapeId": f"shape-{unit['unitId']}-original",
                "editableGroupId": f"group-{unit['unitId']}-editable",
                "notesShapeId": f"shape-{unit['unitId']}-notes",
                "jsonShapeId": f"shape-{unit['unitId']}-json",
            }
            for unit in units
        },
    }


def attach_exported_assets(
    *,
    deep_document_path: Path,
    package_root: Path,
    bucket: str | None = None,
    prefix: str | None = None,
) -> None:
    from docling_serve.deep_document.artifact_writer import write_json

    deep_document = json_load(deep_document_path)
    assets = list(deep_document.get("assets") or [])
    existing_paths = {asset.get("path") for asset in assets if isinstance(asset, dict)}
    for path in sorted(item for item in package_root.rglob("*") if item.is_file()):
        if file_kind(path) != "image":
            continue
        relative_path = path.relative_to(package_root).as_posix()
        if relative_path in existing_paths:
            continue
        asset = {
            "assetId": f"asset-{path.stem}",
            "kind": "exported_image",
            "role": "display",
            "path": relative_path,
            "contentType": mimetypes.guess_type(path.name)[0]
            or "application/octet-stream",
            "sizeBytes": path.stat().st_size,
            "display": True,
        }
        if bucket and prefix is not None:
            asset["s3"] = {
                "bucket": bucket,
                "key": f"{prefix.rstrip('/')}/{relative_path}",
            }
        assets.append(asset)
        existing_paths.add(relative_path)
    deep_document["assets"] = assets
    write_json(deep_document_path, deep_document)


def plain_text(elements: list[dict[str, Any]]) -> str:
    parts = []
    for element in elements:
        text = element.get("text") or {}
        plain = text.get("plain")
        if plain:
            parts.append(str(plain).strip())
    return "\n\n".join(part for part in parts if part)


def source_title(manifest: dict[str, Any]) -> str:
    source = manifest.get("source") or {}
    return str(
        source.get("originalFileName") or source.get("filename") or "Untitled document"
    )


def document_id(manifest: dict[str, Any]) -> str:
    source = manifest.get("source") or {}
    digest = str(source.get("sha256") or "")[:12]
    return f"doc-{digest}" if digest else "doc-unknown"


def common_unit_type(units: list[dict[str, Any]]) -> str:
    values = {unit.get("unitType") for unit in units if unit.get("unitType")}
    return values.pop() if len(values) == 1 else "mixed"


def raw_artifacts(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "xmlParts": manifest.get("xmlParts") or {},
        "theme": manifest.get("theme") or {},
    }


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".html":
        return "html"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return "image"
    if suffix == ".zip":
        return "archive"
    if suffix == ".txt":
        return "text"
    return "file"


def json_load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}
