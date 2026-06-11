from __future__ import annotations

from docling.datamodel.base_models import OutputFormat
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling_core.types.doc.base import ImageRefMode

DEEP_DOCUMENT_FORMATS: tuple[OutputFormat, ...] = (
    OutputFormat.MARKDOWN,
    OutputFormat.JSON,
    OutputFormat.HTML,
    OutputFormat.TEXT,
)


def deep_extraction_mode(value: str | None) -> bool:
    return str(value or "default").strip().lower() == "deep"


def prepare_deep_convert_options(
    options: ConvertDocumentsOptions,
) -> ConvertDocumentsOptions:
    """Return options required by the deep-document contract.

    Deep extraction is S3/package-manifest oriented, so callers should not need
    to remember the richer Docling export profile. Preserve any extra formats
    the caller requested, but always include the contract formats and referenced
    image assets.
    """
    formats = list(options.to_formats or [])
    for required in DEEP_DOCUMENT_FORMATS:
        if required not in formats:
            formats.append(required)

    return options.model_copy(
        update={
            "to_formats": formats,
            "image_export_mode": ImageRefMode.REFERENCED,
        }
    )
