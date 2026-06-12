"""Technical Order (TO) / IPB extractor.

Born-digital AF Technical Orders carry their parts lists as fixed-width layout
text. Generic document extraction loses column alignment and the dot-indenture
hierarchy, so this extractor reads the PDF with ``pdftotext -layout``, triages
the document autonomously, parses title-page identity + MPL tables, rasterises
figure pages, and publishes a ``captify.bom.v1`` sidecar alongside the standard
deep-document payload.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from docling_serve.deep_document.artifact_writer import write_json
from docling_serve.deep_document.schema_validation import validate_artifact
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)
from docling_serve.extractors.docling_extractor import build_docling_structured
from docling_serve.extractors.technical_order.bundle import (
    BOM_SCHEMA_ID,
    build_bom_payload,
)
from docling_serve.extractors.technical_order.metadata import parse_to_metadata
from docling_serve.extractors.technical_order.mpl import parse_parts_lists
from docling_serve.extractors.technical_order.pdftext import (
    ocr_available,
    ocr_page_texts,
    page_layout_texts,
)
from docling_serve.extractors.technical_order.rowbox import attach_row_boxes
from docling_serve.extractors.technical_order.triage import triage_pdf

_log = logging.getLogger(__name__)

TO_PROFILES = {
    "technical-order",
    "technical_order",
    "technicalorder",
    "to",
    "to-ipb",
    "ipb",
}
TO_SUFFIXES = {".pdf"}
_MPL_FAMILIES = {"mpl-modern", "mpl-legacy"}
_TO_DOCUMENT_TYPES = {"TO-IPB", "TO-RPSTL"}
_FIGURE_RENDER_DPI = 150


class TechnicalOrderExtractor(Extractor):
    name = "extract_technical_order"

    def supports(self, ctx: ExtractionContext) -> bool:
        suffix = ctx.source_path.suffix.lower()
        if suffix not in TO_SUFFIXES:
            return False
        profile = (ctx.profile or "default").strip().lower()
        if profile in TO_PROFILES:
            return True
        if profile == "auto":
            return _looks_like_technical_order(ctx.resolve_source_file())
        return False

    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        source_file = ctx.resolve_source_file()
        if not source_file.is_file():
            raise FileNotFoundError(f"Technical order PDF not found: {source_file}")

        ctx.report_progress("to_triage")
        triage = triage_pdf(source_file)
        notes: list[str] = []
        warnings: list[str] = []

        if triage.extraction_class != "born-digital":
            warnings.append(
                f"extraction_class={triage.extraction_class}; "
                "layout parser may be unreliable — OCR path not implemented in v1"
            )

        ctx.report_progress("to_layout_text", page_count=triage.page_count)
        pages = page_layout_texts(source_file)

        ctx.report_progress("to_metadata")
        metadata = parse_to_metadata(pages, filename=ctx.source_path.name)

        ctx.report_progress("to_mpl_parse")
        entries, figures = parse_parts_lists(pages)

        # OCR fallback: scanned documents whose embedded text layer (if any)
        # yielded no parts list get a fresh tesseract pass; keep whichever
        # source parsed more rows.
        text_layer_source = True
        if triage.extraction_class != "born-digital" and not entries and ocr_available():
            ctx.report_progress("to_ocr", page_count=triage.page_count)
            try:
                ocr_pages = ocr_page_texts(source_file)
            except Exception as err:  # noqa: BLE001 — OCR is best-effort
                warnings.append(f"tesseract OCR failed: {err}")
            else:
                ocr_entries, ocr_figures = parse_parts_lists(ocr_pages)
                if len(ocr_entries) > len(entries):
                    entries, figures = ocr_entries, ocr_figures
                    metadata = parse_to_metadata(ocr_pages, filename=ctx.source_path.name)
                    notes.append(f"text source: tesseract OCR ({len(entries)} rows)")
                    text_layer_source = False

        # Row boxes for in-page highlighting (text-layer coordinates only —
        # a tesseract parse has no PDF-space geometry to match against).
        if entries and text_layer_source:
            ctx.report_progress("to_row_boxes")
            boxed = attach_row_boxes(entries, source_file)
            if boxed < len(entries):
                notes.append(f"row boxes matched {boxed}/{len(entries)} entries")

        figures_dir = ctx.bundle_dir / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        ctx.report_progress("to_figure_render", figure_count=len(figures))
        artifacts = _render_figure_pages(
            source_file,
            figures,
            figures_dir,
            bundle_dir=ctx.bundle_dir,
            warnings=warnings,
        )

        # Clickable callouts: OCR each rendered sheet for its index labels,
        # validated against the MPL's index set for that figure.
        ctx.report_progress("to_figure_hotspots")
        _detect_hotspots(figures, entries, figures_dir=ctx.bundle_dir)

        bom_payload = build_bom_payload(
            pdf_path=source_file,
            triage=triage,
            metadata=metadata,
            entries=entries,
            figures=figures,
            source_key=ctx.source_manifest_key,
        )
        bom_path = ctx.bundle_dir / "bom.json"
        write_json(bom_path, bom_payload)
        artifacts.append(bom_path.relative_to(ctx.bundle_dir).as_posix())

        structured = self._structural_base(ctx)
        structured["technicalOrder"] = {
            "schema": BOM_SCHEMA_ID,
            "bom": bom_path.relative_to(ctx.bundle_dir).as_posix(),
            "figures": [f.as_dict() for f in figures],
            "entryCount": len(entries),
            "figureCount": len(figures),
            "documentNumber": metadata.document_number,
            "documentType": triage.document_type,
            "formatFamily": triage.format_family,
        }
        validate_artifact(structured, "deep-document.schema.json")

        if triage.format_family not in _MPL_FAMILIES and entries:
            warnings.append(
                f"format_family={triage.format_family} but {len(entries)} MPL rows parsed"
            )

        return ExtractorResult(
            structured=structured,
            extractor=self.name,
            domain="technical-order",
            artifacts=artifacts,
            manifest_extra={
                "technicalOrder": {
                    "schema": BOM_SCHEMA_ID,
                    "bom": bom_path.relative_to(ctx.bundle_dir).as_posix(),
                    "figuresDir": figures_dir.relative_to(ctx.bundle_dir).as_posix(),
                    "entryCount": len(entries),
                    "figureCount": len(figures),
                    "needsReviewCount": bom_payload["stats"]["needsReviewCount"],
                    "documentNumber": metadata.document_number,
                    "documentType": triage.document_type,
                    "formatFamily": triage.format_family,
                    "extractionClass": triage.extraction_class,
                }
            },
            notes=notes + warnings,
        )

    def _structural_base(self, ctx: ExtractionContext) -> dict[str, Any]:
        if ctx.conv_res is not None and ctx.conv_res.document is not None:
            try:
                return build_docling_structured(ctx)
            except Exception:
                _log.warning(
                    "Docling structural base failed for technical order %s; "
                    "emitting minimal document",
                    ctx.source_path,
                    exc_info=True,
                )
        return _minimal_structured(ctx)


def _looks_like_technical_order(pdf_path: Path) -> bool:
    """Cheap autonomous gate for ``profile=auto`` dispatch."""
    try:
        triage = triage_pdf(pdf_path)
    except Exception:
        return False
    if triage.document_type in _TO_DOCUMENT_TYPES:
        return True
    if triage.format_family in _MPL_FAMILIES:
        return True
    return False


def _render_figure_pages(
    pdf_path: Path,
    figures: list[Any],
    figures_dir: Path,
    *,
    bundle_dir: Path,
    warnings: list[str],
) -> list[str]:
    """Rasterise each page that carries a figure caption (figure-level linkage v1)."""
    if not figures:
        return []

    page_numbers = sorted({fig.page_number for fig in figures if fig.page_number > 0})
    rendered: dict[int, str] = {}
    artifacts: list[str] = []

    for page_no in page_numbers:
        prefix = figures_dir / f"page-{page_no:03d}"
        cmd = [
            "pdftoppm",
            "-png",
            "-r",
            str(_FIGURE_RENDER_DPI),
            "-f",
            str(page_no),
            "-l",
            str(page_no),
            "-singlefile",
            str(pdf_path),
            str(prefix),
        ]
        try:
            subprocess.run(cmd, capture_output=True, check=True, timeout=120)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as err:
            _log.warning("pdftoppm failed for page %s of %s: %s", page_no, pdf_path, err)
            warnings.append(f"figure render failed for page {page_no} ({err})")
            continue
        png_path = prefix.with_suffix(".png")
        if not png_path.is_file():
            warnings.append(f"figure render missing output for page {page_no}")
            continue
        rel = png_path.relative_to(bundle_dir).as_posix()
        rendered[page_no] = rel
        artifacts.append(rel)

    for fig in figures:
        if fig.page_number in rendered:
            fig.media_key = rendered[fig.page_number]

    return artifacts


def _detect_hotspots(figures: list[Any], entries: list[Any], *, figures_dir: Path) -> None:
    """Attach clickable callout positions to every rendered figure sheet."""
    from docling_serve.extractors.technical_order.figure_hotspots import (
        detect_figure_hotspots,
    )

    indices_by_figure: dict[str, set[str]] = {}
    for entry in entries:
        if entry.figure_number_raw and entry.figure_index_raw:
            indices_by_figure.setdefault(entry.figure_number_raw, set()).add(
                entry.figure_index_raw
            )
    for fig in figures:
        if not fig.media_key:
            continue
        valid = indices_by_figure.get(fig.figure_number)
        if not valid:
            continue
        png = figures_dir / fig.media_key
        if not png.is_file():
            continue
        fig.hotspots = [spot.as_dict() for spot in detect_figure_hotspots(png, valid)]


def _minimal_structured(ctx: ExtractionContext) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "schemaVersion": "1.0",
        "artifactKind": "deep_document",
        "documentId": "doc-technical-order",
        "sourceManifestKey": ctx.source_manifest_key,
        "createdAt": datetime.now(UTC).isoformat(),
        "source": {
            "originalFileName": ctx.source_path.name,
            "fileKind": "technical-order",
        },
        "storage": {
            "layout": "relative_object_tree",
            "manifestPath": "deep-document.json",
        },
        "document": {
            "title": ctx.source_path.stem,
            "unitCount": 0,
            "unitType": "technical-order",
            "units": [],
        },
        "assets": [],
        "canvas": {"provider": "tldraw", "shapeMap": {}},
        "rawArtifacts": {},
        "provenance": {
            "generator": "docling_serve.extractors.technical_order_extractor"
        },
        "errors": [],
    }
