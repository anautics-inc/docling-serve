"""AUDIT F4 — layout diagnostics for the manifest geometry.

The HTML preview exposed fidelity problems (overlapping titles, clipped text,
near-empty canvases). Rather than hand-wave them, this module *measures* them
from the manifest's block geometry: overlapping text boxes, blocks clipped
past the page edge, and blank-canvas area. The numbers go into the manifest so
the limitations are visible and trackable, and so a future tldraw/canvas
renderer knows which units need a normalized render contract.

Section units (DOCX) have no geometry and are reported as `geometry: false`.
"""
from __future__ import annotations

from typing import Any

# Thresholds — named parameters, not magic literals.
DEFAULT_OVERLAP_MIN_RATIO = 0.25  # overlap >= 25% of the smaller box counts
DEFAULT_CLIP_TOLERANCE_EMU = 9525  # ~1px at 96dpi — ignore hairline overruns
DEFAULT_HIGH_BLANK_RATIO = 0.75  # a unit >75% empty is flagged sparse


def _area(bbox: dict[str, Any]) -> float:
    return max(0.0, float(bbox.get("cx", 0))) * max(0.0, float(bbox.get("cy", 0)))


def _overlap_area(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax, ay, acx, acy = a.get("x", 0), a.get("y", 0), a.get("cx", 0), a.get("cy", 0)
    bx, by, bcx, bcy = b.get("x", 0), b.get("y", 0), b.get("cx", 0), b.get("cy", 0)
    dx = max(0, min(ax + acx, bx + bcx) - max(ax, bx))
    dy = max(0, min(ay + acy, by + bcy) - max(ay, by))
    return float(dx) * float(dy)


def _is_clipped(bbox: dict[str, Any], cx: int, cy: int, tol: int) -> bool:
    return (
        bbox.get("x", 0) < -tol
        or bbox.get("y", 0) < -tol
        or bbox.get("x", 0) + bbox.get("cx", 0) > cx + tol
        or bbox.get("y", 0) + bbox.get("cy", 0) > cy + tol
    )


def analyze_unit_layout(
    unit: dict[str, Any],
    *,
    overlap_min_ratio: float = DEFAULT_OVERLAP_MIN_RATIO,
    clip_tolerance_emu: int = DEFAULT_CLIP_TOLERANCE_EMU,
) -> dict[str, Any]:
    """Per-unit layout health. `geometry: false` for section (DOCX) units."""
    size = unit.get("pageSizeEmu") or {}
    cx, cy = int(size.get("cx", 0)), int(size.get("cy", 0))
    if cx <= 0 or cy <= 0:
        return {"unitId": unit["unitId"], "geometry": False}

    blocks = unit.get("blocks", [])
    text_boxes = [b["bbox"] for b in blocks if b.get("kind") == "text" and b.get("bbox")]

    overlapping = 0
    for i in range(len(text_boxes)):
        for j in range(i + 1, len(text_boxes)):
            overlap = _overlap_area(text_boxes[i], text_boxes[j])
            smaller = min(_area(text_boxes[i]), _area(text_boxes[j]))
            if smaller > 0 and overlap / smaller >= overlap_min_ratio:
                overlapping += 1

    clipped = sum(
        1 for b in blocks if b.get("bbox") and _is_clipped(b["bbox"], cx, cy, clip_tolerance_emu)
    )
    covered = sum(_area(b["bbox"]) for b in blocks if b.get("bbox"))
    blank_ratio = max(0.0, 1.0 - min(1.0, covered / (cx * cy)))

    return {
        "unitId": unit["unitId"],
        "geometry": True,
        "overlappingTextPairs": overlapping,
        "clippedBlocks": clipped,
        "blankAreaRatio": round(blank_ratio, 3),
    }


def analyze_manifest_layout(manifest: dict[str, Any]) -> dict[str, Any]:
    """Aggregate layout health across all units — goes into manifest diagnostics."""
    per_unit = [analyze_unit_layout(u) for u in manifest.get("units", [])]
    geom = [u for u in per_unit if u.get("geometry")]
    return {
        "geometryUnits": len(geom),
        "sectionUnits": len(per_unit) - len(geom),
        "unitsWithOverlap": sum(1 for u in geom if u["overlappingTextPairs"] > 0),
        "totalOverlappingTextPairs": sum(u["overlappingTextPairs"] for u in geom),
        "unitsWithClippedBlocks": sum(1 for u in geom if u["clippedBlocks"] > 0),
        "totalClippedBlocks": sum(u["clippedBlocks"] for u in geom),
        "sparseUnits": sum(1 for u in geom if u["blankAreaRatio"] >= DEFAULT_HIGH_BLANK_RATIO),
        # The preview renderer is a DEBUG artifact — these counts measure why
        # it is not yet a production-fidelity viewer (AUDIT F4).
        "rendererStatus": "debug_preview_only",
    }
