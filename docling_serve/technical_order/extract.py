"""Technical Order (IPB / RPSTL) extraction entry point.

A genuine docling gap: the master parts list (MPL) is a column-aligned table
whose meaning lives in its print layout, which docling's reading-order export
does not preserve. So this parses the PDF with poppler's ``pdftotext -layout``
and the deterministic MPL parser, producing the ``captify.bom.v1`` payload. The
*base* document (markdown/json/chunks) is still docling's native job — this runs
as an additional, domain-specific pass.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from docling_serve.technical_order.bundle import BOM_SCHEMA_ID, build_bom_payload
from docling_serve.technical_order.metadata import parse_to_metadata
from docling_serve.technical_order.mpl import parse_parts_lists
from docling_serve.technical_order.pdftext import (
    ocr_available,
    ocr_page_texts,
    page_layout_texts,
)
from docling_serve.technical_order.rowbox import attach_row_boxes
from docling_serve.technical_order.triage import triage_pdf

_log = logging.getLogger(__name__)

TO_PROFILES = {
    "technical-order",
    "technical_order",
    "technicalorder",
    "to",
    "to-ipb",
    "ipb",
}
_TO_DOCUMENT_TYPES = {"TO-IPB", "TO-RPSTL"}
_MPL_FAMILIES = {"mpl-modern", "mpl-legacy"}
# Max page count for the full-document OCR fallback. Above this, tesseract over
# every page costs minutes for documents that are almost always large RPSTL/TM
# formats the column parser can't read regardless of text source.
_OCR_PAGE_BUDGET = 180


def looks_like_technical_order(pdf_path: Path) -> bool:
    """Content-based detection for the ``auto`` profile (cheap triage)."""
    try:
        triage = triage_pdf(pdf_path)
    except Exception:
        return False
    return triage.document_type in _TO_DOCUMENT_TYPES or triage.format_family in _MPL_FAMILIES


def extract_technical_order(
    pdf_path: Path,
    *,
    source_key: str = "",
    media_dir: Path | None = None,
    vision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse an IPB/RPSTL technical-order PDF into the ``captify.bom.v1`` payload.

    When ``media_dir`` is given, each figure sheet is rendered to a PNG there and
    its index callouts are detected + linked to the parts list both ways (callout
    -> part on the figure, part -> callout box on the entry), so the UI can jump
    image<->part and re-stamp callouts when references change.
    """
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Technical order PDF not found: {pdf_path}")

    warnings: list[str] = []
    notes: list[str] = []

    triage = triage_pdf(pdf_path)
    if triage.extraction_class != "born-digital":
        warnings.append(
            f"extraction_class={triage.extraction_class}; layout parser may be unreliable "
            "(OCR path is a best-effort fallback)"
        )

    pages = page_layout_texts(pdf_path)
    metadata = parse_to_metadata(pages, filename=pdf_path.name)
    entries, figures = parse_parts_lists(pages)

    # OCR fallback: a scanned / dirty-legacy-OCR TO whose text layer yielded no
    # parts list gets a fresh tesseract pass (this genuinely recovers rows on
    # older IPBs whose embedded OCR layer the column parser can't align). Two
    # guards keep it from burning minutes for no gain:
    #   * born-digital docs are skipped — their text is clean, so a 0-row parse
    #     means the *format* is unsupported (e.g. an Army TM RPSTL work package),
    #     which re-OCR won't fix.
    #   * documents above the page budget are skipped — full-document tesseract
    #     on a 200-350pp manual costs minutes, and the big ones here are RPSTL/TM
    #     formats the parser doesn't handle anyway.
    text_layer_source = True
    if (
        triage.extraction_class != "born-digital"
        and not entries
        and ocr_available()
        and triage.page_count <= _OCR_PAGE_BUDGET
    ):
        try:
            ocr_pages = ocr_page_texts(pdf_path)
        except Exception as err:
            warnings.append(f"tesseract OCR failed: {err}")
        else:
            ocr_entries, ocr_figures = parse_parts_lists(ocr_pages)
            if len(ocr_entries) > len(entries):
                entries, figures = ocr_entries, ocr_figures
                metadata = parse_to_metadata(ocr_pages, filename=pdf_path.name)
                notes.append(f"text source: tesseract OCR ({len(entries)} rows)")
                text_layer_source = False

    # Row boxes for in-page highlighting (text-layer coordinates only).
    if entries and text_layer_source:
        boxed = attach_row_boxes(entries, pdf_path)
        if boxed < len(entries):
            notes.append(f"row boxes matched {boxed}/{len(entries)} entries")

    if triage.format_family not in _MPL_FAMILIES and entries:
        warnings.append(
            f"format_family={triage.format_family} but {len(entries)} MPL rows parsed"
        )

    # Figure callouts <-> parts (clickable hotspots). Render each figure sheet,
    # OCR its index callouts, and wire the bidirectional link. Best-effort and
    # only when a media directory is provided (the publish path).
    if media_dir is not None and entries and figures:
        try:
            from docling_serve.technical_order.figure_hotspots import attach_hotspots

            hs_stats = attach_hotspots(pdf_path, entries, figures, media_dir, vision=vision)
            notes.append(
                f"figure hotspots: {hs_stats['hotspots']} callouts on "
                f"{hs_stats['rendered']} sheet(s) "
                f"({hs_stats.get('visionHotspots', 0)} via vision), "
                f"{hs_stats['linkedParts']} parts linked"
            )
        except Exception as err:  # pragma: no cover - rendering env dependent
            warnings.append(f"figure hotspot pass failed: {err}")

    bom = build_bom_payload(
        pdf_path=pdf_path,
        triage=triage,
        metadata=metadata,
        entries=entries,
        figures=figures,
        source_key=source_key,
    )
    return {
        "schema": BOM_SCHEMA_ID,
        "documentNumber": metadata.document_number,
        "documentType": triage.document_type,
        "formatFamily": triage.format_family,
        "extractionClass": triage.extraction_class,
        "entryCount": len(entries),
        "figureCount": len(figures),
        "figures": [f.as_dict() for f in figures],
        "bom": bom,
        "notes": notes,
        "warnings": warnings,
    }
