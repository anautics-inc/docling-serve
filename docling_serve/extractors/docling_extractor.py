"""Default extractor: Docling's reading-order document for everything generic.

Wraps the existing deep-document adapter so PDFs, Word docs, spreadsheets,
text, and images keep their current behaviour. Other extractors (schematic,
access) reuse :func:`build_docling_structured` as a structural base.
"""

from __future__ import annotations

from typing import Any

from docling_serve.deep_document.docling_adapter import (
    manifest_from_docling_document,
)
from docling_serve.deep_document.document_builder import build_deep_document
from docling_serve.deep_document.schema_validation import validate_artifact
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)


def build_docling_structured(ctx: ExtractionContext) -> dict[str, Any]:
    """Build the validated deep-document dict from Docling's conversion result."""
    if ctx.conv_res is None or ctx.conv_res.document is None:
        raise ValueError(
            "DoclingExtractor requires a Docling ConversionResult with a document."
        )
    manifest = manifest_from_docling_document(
        ctx.conv_res.document.model_dump(mode="json"),
        filename=ctx.source_path.name,
        source_manifest_key=ctx.source_manifest_key,
    )
    structured = build_deep_document(
        manifest=manifest, source_manifest_key=ctx.source_manifest_key
    )
    validate_artifact(structured, "deep-document.schema.json")
    return structured


class DoclingExtractor(Extractor):
    """Fallback extractor used when no more specific extractor matches."""

    name = "extract_doc"

    def supports(self, ctx: ExtractionContext) -> bool:  # pragma: no cover - trivial
        # Registry default: matches anything with a Docling conversion result.
        return ctx.conv_res is not None

    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        structured = build_docling_structured(ctx)
        return ExtractorResult(structured=structured, extractor=self.name)
