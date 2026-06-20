"""XFA / AF-form extraction (Adobe LiveCycle dynamic PDF forms)."""

from __future__ import annotations

from docling_serve.form.extract import (
    XFA_PROFILES,
    XfaToolsUnavailableError,
    extract_xfa_form,
    is_xfa_pdf,
    parse_dataset_values,
    parse_template_fields,
    pdf_has_xfa,
    read_xfa_packets,
    xfa_markdown,
)

__all__ = [
    "XFA_PROFILES",
    "XfaToolsUnavailableError",
    "extract_xfa_form",
    "is_xfa_pdf",
    "parse_dataset_values",
    "parse_template_fields",
    "pdf_has_xfa",
    "read_xfa_packets",
    "xfa_markdown",
]
