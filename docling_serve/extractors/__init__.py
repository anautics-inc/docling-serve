"""Extractor registry and dispatch.

Order matters: the first extractor whose :meth:`~Extractor.supports` returns
True wins, so list the most specific (domain/profile-driven) extractors before
the generic Docling fallback.

    select_extractor(ctx).build(ctx) -> ExtractorResult

Register a new format by adding one :class:`Extractor` to ``_REGISTRY``; nothing
in the assembly path needs to change.
"""

from __future__ import annotations

from docling_serve.extractors.access_extractor import AccessExtractor
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)
from docling_serve.extractors.docling_extractor import DoclingExtractor
from docling_serve.extractors.pptx_extractor import PptxExtractor
from docling_serve.extractors.schematic_extractor import SchematicExtractor
from docling_serve.extractors.xfa_extractor import XfaFormExtractor

# Most specific first; DoclingExtractor is the catch-all fallback.
# XfaFormExtractor leads: XFA detection is content-based (an ordinary-looking
# .pdf whose real form lives in XML packets docling cannot see).
_REGISTRY: list[Extractor] = [
    XfaFormExtractor(),
    AccessExtractor(),
    SchematicExtractor(),
    PptxExtractor(),
]

_FALLBACK: Extractor = DoclingExtractor()


def registered_extractors() -> list[Extractor]:
    return [*_REGISTRY, _FALLBACK]


def select_extractor(ctx: ExtractionContext) -> Extractor:
    """Pick the extractor that owns ``ctx`` (falls back to Docling)."""
    for extractor in _REGISTRY:
        if extractor.supports(ctx):
            return extractor
    return _FALLBACK


__all__ = [
    "AccessExtractor",
    "DoclingExtractor",
    "ExtractionContext",
    "Extractor",
    "ExtractorResult",
    "PptxExtractor",
    "SchematicExtractor",
    "XfaFormExtractor",
    "registered_extractors",
    "select_extractor",
]
