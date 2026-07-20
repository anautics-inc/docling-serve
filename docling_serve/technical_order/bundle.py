"""Assemble the additive ``captify.bom.v2`` Technical Order payload."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from docling_serve.technical_order.contract import (
    BOM_SCHEMA_ID,
    LEGACY_BOM_SCHEMA_ID,
    inherited_markings,
    provenance,
    stable_id,
)
from docling_serve.technical_order.metadata import TOMetadata
from docling_serve.technical_order.mpl import FigureRecord, PartsListEntry
from docling_serve.technical_order.triage import TriageResult


def build_bom_payload(
    *,
    pdf_path: Path,
    triage: TriageResult,
    metadata: TOMetadata,
    entries: list[PartsListEntry],
    figures: list[FigureRecord],
    source_key: str = "",
) -> dict[str, Any]:
    source_sha256 = _sha256(pdf_path)
    document_id = stable_id("technical-order", source_sha256, metadata.document_number)
    publication_id = stable_id(
        "publication",
        "technical-order",
        metadata.document_number.strip().upper(),
    )
    revision = metadata.change_level or "basic"
    issue_kind = (
        triage.document_kind
        if triage.document_kind
        in {"basic", "change", "supplement", "merged-basic", "unknown"}
        else "unknown"
    )
    document_markings = (
        {
            "distributionStatement": metadata.distribution_statement,
            "sourceField": "document.distributionStatement",
        }
        if metadata.distribution_statement
        else {}
    )
    _assign_entity_contract_fields(
        source_sha256=source_sha256,
        document_id=document_id,
        markings=document_markings,
        entries=entries,
        figures=figures,
    )
    needs_review = sum(1 for e in entries if e.review_status == "needs-review")
    by_type: dict[str, int] = {}
    for e in entries:
        by_type[e.row_type] = by_type.get(e.row_type, 0) + 1
    return {
        "id": document_id,
        "schema": BOM_SCHEMA_ID,
        "compatibleSchemas": [LEGACY_BOM_SCHEMA_ID],
        "source": {
            "filename": pdf_path.name,
            # Landing object key of the original PDF (production ingest) —
            # hydration copies it into the tenant files bucket so the UI can
            # open the PDF at the exact referenced page.
            "sourceKey": source_key,
            "sha256": source_sha256,
            "sizeBytes": pdf_path.stat().st_size,
            "pageCount": triage.page_count,
            "producer": triage.producer,
            "creator": triage.creator,
        },
        "triage": triage.as_dict(),
        "publication": {
            "publicationId": publication_id,
            "documentNumber": metadata.document_number,
            "title": metadata.document_title,
            "publicationType": triage.document_type,
        },
        "publicationIssue": {
            "issueId": document_id,
            "revision": revision,
            "issueKind": issue_kind,
            "publicationDate": metadata.publication_date or None,
        },
        "document": {
            **metadata.as_dict(),
            "id": document_id,
            **({"markings": document_markings} if document_markings else {}),
        },
        "figures": [f.as_dict() for f in figures],
        "figureGroups": build_figure_groups(figures),
        "entries": [e.as_dict() for e in entries],
        "stats": {
            "entryCount": len(entries),
            "figureCount": len(figures),
            "needsReviewCount": needs_review,
            "rowTypeCounts": by_type,
            "maxIndentureLevel": max((e.indenture_level for e in entries), default=0),
        },
        "provenance": provenance(
            method="deterministic-pipeline",
            parser="docling-serve.technical-order",
            version="2",
            confidence=None,
        ),
    }


def _assign_entity_contract_fields(
    *,
    source_sha256: str,
    document_id: str,
    markings: dict[str, Any],
    entries: list[PartsListEntry],
    figures: list[FigureRecord],
) -> None:
    """Assign deterministic IDs and inherited markings before serialization."""
    for position, figure in enumerate(figures):
        figure.stable_id = stable_id(
            "figure-sheet",
            source_sha256,
            figure.figure_number,
            figure.sheet_number or "1",
            figure.page_number,
            position,
        )
        figure.markings = inherited_markings(markings, document_id) or {}
        for hotspot_position, hotspot in enumerate(figure.hotspots):
            confidence = float(hotspot.get("confidence") or 0.0)
            hotspot["confidence"] = round(
                max(
                    0.0,
                    min(1.0, confidence / 100.0 if confidence > 1.0 else confidence),
                ),
                4,
            )
            hotspot["id"] = stable_id(
                "hotspot",
                source_sha256,
                figure.stable_id,
                hotspot.get("index", ""),
                hotspot_position,
            )
            hotspot["figureSheetId"] = figure.stable_id
            geometry = (hotspot.get("provenance") or {}).get("sourceGeometry")
            if geometry is not None:
                geometry["pageNumber"] = figure.page_number
            if figure.markings:
                hotspot["markings"] = inherited_markings(
                    figure.markings, figure.stable_id
                )

    by_sequence: dict[int, PartsListEntry] = {}
    for position, entry in enumerate(entries):
        entry.stable_id = stable_id(
            "parts-list-entry",
            source_sha256,
            entry.page_number,
            entry.figure_number_raw,
            entry.figure_index_raw,
            entry.part_number_raw,
            entry.sequence,
            position,
        )
        entry.markings = inherited_markings(markings, document_id) or {}
        by_sequence[entry.sequence] = entry
    for entry in entries:
        parent = by_sequence.get(entry.parent_sequence or -1)
        entry.parent_id = parent.stable_id if parent else None


def _sheet_sort_key(fig: FigureRecord) -> tuple[int, str]:
    try:
        return (int(fig.sheet_number), "")
    except ValueError:
        return (0, fig.sheet_number)


def build_figure_groups(figures: list[FigureRecord]) -> list[dict[str, Any]]:
    """Group a figure's sheets for structured, editable multi-page overlays.

    A multi-sheet drawing ("Figure 7-1, Sheet 1 of 2" / "Figure 7-1, Sheet
    2") is one logical illustration split across pages, not N unrelated
    figures. Grouping by figure number and ordering by sheet lets a viewer
    compose them as a single drawing (page-turn between sheets, consistent
    callout numbering) instead of surfacing disconnected images — this
    matters most for figure-only documents, whose entire content IS the
    drawing set.
    """
    groups: dict[str, list[FigureRecord]] = {}
    for fig in figures:
        groups.setdefault(fig.figure_number, []).append(fig)

    out: list[dict[str, Any]] = []
    for figure_number, sheets in groups.items():
        ordered = sorted(sheets, key=_sheet_sort_key)
        declared_total = next((s.sheet_total for s in ordered if s.sheet_total), None)
        out.append(
            {
                "id": stable_id(
                    "figure",
                    ordered[0].stable_id or figure_number,
                    figure_number,
                ),
                "figureNumber": figure_number,
                "figureTitle": next(
                    (s.figure_title for s in ordered if s.figure_title), ""
                ),
                "sheetCount": len(ordered),
                "declaredSheetTotal": declared_total,
                "composition": "multi-sheet" if len(ordered) > 1 else "single",
                "sheets": [
                    {
                        "id": s.stable_id,
                        "sheetNumber": s.sheet_number,
                        "pageNumber": s.page_number,
                        "mediaKey": s.media_key,
                    }
                    for s in ordered
                ],
            }
        )
    out.sort(key=lambda g: g["figureNumber"])
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
