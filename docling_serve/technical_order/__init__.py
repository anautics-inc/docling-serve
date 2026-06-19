"""Technical Order (TO) understanding: triage, metadata, parts-list parsing.

Pure functions over a PDF path — no service dependencies — so every module is
testable offline and reusable by the extractor, scripts, and tests. The
assembled output is the ``captify.bom.v1`` bundle (see :mod:`bundle`).
"""

from docling_serve.technical_order.bundle import build_bom_payload
from docling_serve.technical_order.metadata import (
    TOMetadata,
    parse_to_metadata,
)
from docling_serve.technical_order.mpl import (
    PartsListEntry,
    parse_parts_lists,
)
from docling_serve.technical_order.pdftext import page_layout_texts
from docling_serve.technical_order.triage import TriageResult, triage_pdf

__all__ = [
    "PartsListEntry",
    "TOMetadata",
    "TriageResult",
    "build_bom_payload",
    "page_layout_texts",
    "parse_parts_lists",
    "parse_to_metadata",
    "triage_pdf",
]
