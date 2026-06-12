"""Extractor registry and dispatch.

Order matters: the first extractor whose :meth:`~Extractor.supports` returns
True wins, so list the most specific (domain/profile-driven) extractors before
the generic Docling fallback.

    select_extractor(ctx).build(ctx) -> ExtractorResult

Register a new format by adding one :class:`Extractor` to ``_REGISTRY``; nothing
in the assembly path needs to change.
"""

from __future__ import annotations

from docling_serve.extractors.access_extractor import ACCESS_SUFFIXES, AccessExtractor
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)
from docling_serve.extractors.docling_extractor import DoclingExtractor
from docling_serve.extractors.pptx_extractor import PptxExtractor
from docling_serve.extractors.schematic_extractor import SchematicExtractor
from docling_serve.extractors.technical_order_extractor import TechnicalOrderExtractor
from docling_serve.extractors.xfa_extractor import XfaFormExtractor

# Most specific first; DoclingExtractor is the catch-all fallback.
# XfaFormExtractor leads: XFA detection is content-based (an ordinary-looking
# .pdf whose real form lives in XML packets docling cannot see).
_REGISTRY: list[Extractor] = [
    XfaFormExtractor(),
    AccessExtractor(),
    TechnicalOrderExtractor(),
    SchematicExtractor(),
    PptxExtractor(),
]

_FALLBACK: Extractor = DoclingExtractor()

#: Suffixes owned by registry extractors that read the source bytes natively
#: and never need a docling conversion. Uploads with these suffixes must have
#: their raw bytes persisted to a scratch dir so the extractor can re-open the
#: true file by name (docling only ever sees an in-memory stream).
NON_DOCLING_SUFFIXES: frozenset[str] = frozenset(ACCESS_SUFFIXES)


def registered_extractors() -> list[Extractor]:
    return [*_REGISTRY, _FALLBACK]


def select_extractor(ctx: ExtractionContext) -> Extractor:
    """Pick the extractor that owns ``ctx`` (falls back to Docling)."""
    return select_registry_extractor(ctx) or _FALLBACK


def select_registry_extractor(ctx: ExtractionContext) -> Extractor | None:
    """The registry (non-docling-fallback) extractor owning ``ctx``, if any.

    This is the pipeline gate for sources docling cannot convert: a failed /
    skipped docling conversion is still extractable when one of these
    extractors produces units from the raw source bytes.
    """
    for extractor in _REGISTRY:
        if extractor.supports(ctx):
            return extractor
    return None


__all__ = [
    "NON_DOCLING_SUFFIXES",
    "AccessExtractor",
    "DoclingExtractor",
    "ExtractionContext",
    "Extractor",
    "ExtractorResult",
    "PptxExtractor",
    "SchematicExtractor",
    "TechnicalOrderExtractor",
    "XfaFormExtractor",
    "registered_extractors",
    "select_extractor",
    "select_registry_extractor",
]
