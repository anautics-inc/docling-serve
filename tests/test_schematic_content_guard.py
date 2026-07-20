"""Non-schematic content guard: refuse mechanical exploded-view / parts-
breakdown drawings instead of running them through the "components + nets"
schematic model prompt, which would otherwise fabricate a plausible-looking
but meaningless circuit graph for artwork that has no circuit to trace.
"""

import zlib
from pathlib import Path

from docling_serve.schematic.content_guard import (
    classify_drawing_content,
    has_raster_image,
)
from docling_serve.schematic.schematic_extractor import _looks_like_vector_drawing

EXPLODED_VIEW_CAPTION = "Figure 7-1. Control Unit - Exploded View (Sheet 1 of 2)"
SCHEMATIC_CAPTION = "Figure 4-2. Power Supply Schematic Diagram"
PARTS_BREAKDOWN_CAPTION = (
    "SECTION II ILLUSTRATED PARTS BREAKDOWN Figure 9-1. Control Unit"
)


def test_raster_exploded_view_caption_is_refused():
    verdict = classify_drawing_content(
        raster_backed=True, page_text=EXPLODED_VIEW_CAPTION
    )
    assert verdict.is_non_schematic is True
    assert "exploded-view" in verdict.reason


def test_vector_page_is_never_refused_even_with_exploded_view_caption():
    """A real schematic is never raster-backed; the guard only fires on scans/photos."""
    verdict = classify_drawing_content(
        raster_backed=False, page_text=EXPLODED_VIEW_CAPTION
    )
    assert verdict.is_non_schematic is False


def test_raster_page_without_exploded_view_language_is_not_refused():
    verdict = classify_drawing_content(raster_backed=True, page_text="Figure 4-2")
    assert verdict.is_non_schematic is False


def test_schematic_vocabulary_overrides_exploded_view_language():
    text = f"{EXPLODED_VIEW_CAPTION} {SCHEMATIC_CAPTION}"
    verdict = classify_drawing_content(raster_backed=True, page_text=text)
    assert verdict.is_non_schematic is False


def test_refdes_shaped_token_overrides_exploded_view_language():
    text = f"{EXPLODED_VIEW_CAPTION} see R1 and C12 for details"
    verdict = classify_drawing_content(raster_backed=True, page_text=text)
    assert verdict.is_non_schematic is False


def test_parts_breakdown_caption_is_refused():
    verdict = classify_drawing_content(
        raster_backed=True, page_text=PARTS_BREAKDOWN_CAPTION
    )
    assert verdict.is_non_schematic is True


def test_no_page_text_is_not_refused():
    """Ambiguity (no OCR/text-layer signal at all) resolves to letting the
    model try — the guard only catches the CLEAR case."""
    verdict = classify_drawing_content(raster_backed=True, page_text="")
    assert verdict.is_non_schematic is False


def test_has_raster_image_true_for_figure_only_pdf_fixture():
    fixture = (
        Path(__file__).resolve().parents[1] / "docs" / "tests" / "Test 1 figures.pdf"
    )
    if not fixture.is_file():
        import pytest

        pytest.skip(f"fixture not available: {fixture}")
    assert has_raster_image(fixture) is True


def test_has_raster_image_false_for_missing_file():
    assert has_raster_image(Path("/nonexistent/does-not-exist.pdf")) is False


def test_auto_router_skips_decompression_bomb(tmp_path):
    expanded = b"0 0 l " * 400_000
    stream = zlib.compress(expanded, level=9)
    pdf = tmp_path / "bomb.pdf"
    pdf.write_bytes(b"%PDF-1.4\nstream\n" + stream + b"\nendstream\n")
    assert len(stream) < 20_000
    assert _looks_like_vector_drawing(pdf) is False
