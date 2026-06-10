"""Deep document extraction — pure structural normalization of Docling output.

This package produces the unified ``deep-document.json`` object (units,
elements, geometry, images, canvas) for the per-request ``extraction=deep``
mode. It contains no course-model or pedagogical inference; that is a separate
concern handled outside deep extraction.
"""

from docling_serve.deep_document.document_builder import build_deep_document

__all__ = ["build_deep_document"]
