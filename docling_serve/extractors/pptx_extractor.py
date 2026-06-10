"""Presentation extractor: native python-pptx geometry with a Docling fallback.

Preserves the prior behaviour where ``.ppt``/``.pptx`` are parsed with
python-pptx (real per-slide geometry + media) and fall back to Docling's
structure if native parsing fails, so the pipeline never drops a deck.
"""

from __future__ import annotations

import logging

from docling_serve.deep_document.document_builder import build_deep_document
from docling_serve.deep_document.pptx_adapter import manifest_from_pptx
from docling_serve.deep_document.schema_validation import validate_artifact
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)
from docling_serve.extractors.docling_extractor import build_docling_structured

_log = logging.getLogger(__name__)

PRESENTATION_SUFFIXES = {".ppt", ".pptx"}


class PptxExtractor(Extractor):
    name = "extract_ppt"

    def supports(self, ctx: ExtractionContext) -> bool:
        return ctx.source_path.suffix.lower() in PRESENTATION_SUFFIXES

    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        pptx_path = ctx.resolve_source_file()
        try:
            manifest = manifest_from_pptx(
                pptx_path,
                filename=ctx.source_path.name,
                source_manifest_key=ctx.source_manifest_key,
                media_dir=ctx.media_dir,
                asset_path_prefix="media",
            )
            structured = build_deep_document(
                manifest=manifest, source_manifest_key=ctx.source_manifest_key
            )
            validate_artifact(structured, "deep-document.schema.json")
            return ExtractorResult(structured=structured, extractor=self.name)
        except Exception:
            _log.exception(
                "python-pptx extraction failed for %s (resolved=%s); "
                "falling back to Docling structure",
                ctx.source_path,
                pptx_path,
            )
            structured = build_docling_structured(ctx)
            return ExtractorResult(
                structured=structured,
                extractor=self.name,
                notes=["pptx_native_failed_fell_back_to_docling"],
            )
