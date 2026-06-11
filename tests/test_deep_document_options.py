from __future__ import annotations

from docling.datamodel.base_models import OutputFormat
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling_core.types.doc.base import ImageRefMode

from docling_serve.deep_document.options import (
    deep_extraction_mode,
    prepare_deep_convert_options,
)


def test_deep_extraction_mode_is_explicit() -> None:
    assert deep_extraction_mode("deep") is True
    assert deep_extraction_mode(" Deep ") is True
    assert deep_extraction_mode(None) is False
    assert deep_extraction_mode("default") is False


def test_deep_options_force_contract_exports_without_dropping_extras() -> None:
    options = ConvertDocumentsOptions(
        to_formats=[OutputFormat.DOCTAGS],
        image_export_mode=ImageRefMode.EMBEDDED,
    )

    deep_options = prepare_deep_convert_options(options)

    assert deep_options.image_export_mode == ImageRefMode.REFERENCED
    assert deep_options.to_formats == [
        OutputFormat.DOCTAGS,
        OutputFormat.MARKDOWN,
        OutputFormat.JSON,
        OutputFormat.HTML,
        OutputFormat.TEXT,
    ]
    assert options.image_export_mode == ImageRefMode.EMBEDDED
