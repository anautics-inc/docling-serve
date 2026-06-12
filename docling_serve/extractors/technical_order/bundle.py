"""Assemble the ``captify.bom.v1`` payload from triage + metadata + entries."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from docling_serve.extractors.technical_order.metadata import TOMetadata
from docling_serve.extractors.technical_order.mpl import FigureRecord, PartsListEntry
from docling_serve.extractors.technical_order.triage import TriageResult

BOM_SCHEMA_ID = "captify.bom.v1"


def build_bom_payload(
    *,
    pdf_path: Path,
    triage: TriageResult,
    metadata: TOMetadata,
    entries: list[PartsListEntry],
    figures: list[FigureRecord],
    source_key: str = "",
) -> dict[str, Any]:
    needs_review = sum(1 for e in entries if e.review_status == "needs-review")
    by_type: dict[str, int] = {}
    for e in entries:
        by_type[e.row_type] = by_type.get(e.row_type, 0) + 1
    return {
        "schema": BOM_SCHEMA_ID,
        "source": {
            "filename": pdf_path.name,
            # Landing object key of the original PDF (production ingest) —
            # hydration copies it into the tenant files bucket so the UI can
            # open the PDF at the exact referenced page.
            "sourceKey": source_key,
            "sha256": _sha256(pdf_path),
            "sizeBytes": pdf_path.stat().st_size,
            "pageCount": triage.page_count,
            "producer": triage.producer,
            "creator": triage.creator,
        },
        "triage": triage.as_dict(),
        "document": metadata.as_dict(),
        "figures": [f.as_dict() for f in figures],
        "entries": [e.as_dict() for e in entries],
        "stats": {
            "entryCount": len(entries),
            "figureCount": len(figures),
            "needsReviewCount": needs_review,
            "rowTypeCounts": by_type,
            "maxIndentureLevel": max((e.indenture_level for e in entries), default=0),
        },
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
