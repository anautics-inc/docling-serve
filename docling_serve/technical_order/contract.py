"""Shared helpers for the Technical Order v2 contracts."""

from __future__ import annotations

import hashlib
from typing import Any

TO_SCHEMA_ID = "captify.to.v2"
LEGACY_TO_SCHEMA_ID = "captify.to-content.v1"
BOM_SCHEMA_ID = "captify.bom.v2"
LEGACY_BOM_SCHEMA_ID = "captify.bom.v1"


def stable_id(kind: str, namespace: str, *identity: object) -> str:
    """Return an opaque, deterministic ID scoped to a source document."""
    material = "\x1f".join((namespace, kind, *(str(value) for value in identity)))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{kind}_{digest}"


def source_geometry(
    page_number: int, box: tuple[float, float, float, float] | list[float] | None = None
) -> dict[str, Any]:
    """Describe source coordinates without inventing a bounding box."""
    geometry: dict[str, Any] = {"pageNumber": page_number}
    if box is not None:
        geometry.update(
            {
                "coordinateSystem": "normalized-page-top-left",
                "boundingBox": list(box),
            }
        )
    return geometry


def provenance(
    *,
    method: str,
    parser: str,
    version: str,
    confidence: float | None,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the common extraction provenance envelope."""
    value: dict[str, Any] = {
        "method": method,
        "parser": {"name": parser, "version": version},
    }
    if confidence is not None:
        value["confidence"] = round(max(0.0, min(1.0, confidence)), 4)
    if geometry is not None:
        value["sourceGeometry"] = geometry
    return value


def inherited_markings(
    markings: dict[str, Any] | None, parent_id: str
) -> dict[str, Any] | None:
    """Copy source markings onto a child and record the propagation edge."""
    if not markings:
        return None
    inherited = dict(markings)
    inherited["inheritedFrom"] = parent_id
    return inherited
