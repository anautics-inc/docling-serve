"""Regression tests for the technical-order pipeline against real IPB PDFs.

Fixtures live in ``docs/tests/`` (outside the packaged ``tests/`` tree since
they are large real-world manuals, not synthetic unit fixtures):

- ``2JA Test 1.pdf`` / ``2JA8-28-2.pdf`` — the same IPB content as a
  dirty-text-layer scan and a no-text-layer scan, both mpl-modern parts
  lists with multi-sheet exploded-view figures.
- ``Test 1 figures.pdf`` — a figure-only appendix: five pages, each just a
  bare ``Figure 3-N`` caption over a rendered drawing, no parts table at all.

These pin the specific bugs fixed in this pass: explanatory legend/banner
prose leaking into BOM rows, bare figure captions, and figure-only documents
returning zero figures / publishing nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docling_serve.technical_order.extract import extract_technical_order
from docling_serve.technical_order.mpl import (
    _USABLE_ON_LEGEND_ROW,
    parse_parts_lists,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "docs" / "tests"

MPL_FIXTURES = ["2JA Test 1.pdf", "2JA8-28-2.pdf"]


def _require_fixture(name: str) -> Path:
    path = FIXTURE_DIR / name
    if not path.is_file():
        pytest.skip(f"fixture not available: {path}")
    return path


@pytest.mark.parametrize("filename", MPL_FIXTURES)
def test_mpl_pdf_parses_expected_row_and_figure_counts(filename):
    """Pin the parsed row/figure counts for both text-source variants of the
    same manual so a parser regression shows up as a count change."""
    result = extract_technical_order(_require_fixture(filename))

    assert result["documentType"] == "TO-IPB"
    assert result["entryCount"] == 412
    assert result["figureCount"] == 31


@pytest.mark.parametrize("filename", MPL_FIXTURES)
def test_mpl_pdf_has_no_legend_or_banner_pollution(filename):
    """Regression guard: the per-assembly 'CODES ... USABLE ON' legend and the
    bare figure-number banner ahead of an end-item row must never appear as
    BOM row data or corrupt an unrelated entry's description (see mpl.py's
    ``_USABLE_ON_LEGEND_HEADER`` / ``_FIGURE_BANNER`` handling)."""
    result = extract_technical_order(_require_fixture(filename))
    entries = result["bom"]["entries"]

    polluted = [
        e
        for e in entries
        if "USABLE ON" in e["descriptionRaw"].upper()
        or "CODES" in e["descriptionRaw"].upper()
    ]
    assert polluted == []

    needs_review = [e for e in entries if e["reviewStatus"] == "needs-review"]
    assert needs_review == []


def test_mpl_pdf_attributes_entry_to_new_figure_after_banner_row():
    """The figure-declaration banner row ('9-4- ... CIRCUIT CARD ASSEMBLY
    (A3)') must switch the current figure rather than being merged into the
    previous figure's last (unrelated) entry."""
    result = extract_technical_order(_require_fixture("2JA Test 1.pdf"))
    entries = result["bom"]["entries"]

    # "123D7316G1" appears twice in the document (once as a REF listing under
    # figure 9-1, once as the entry right after the 9-4 banner) — disambiguate
    # on the description, which is unique to the post-banner entry.
    match = next(
        (
            e
            for e in entries
            if e["partNumberRaw"] == "123D7316G1" and "FOR NHA" in e["descriptionRaw"]
        ),
        None,
    )
    assert match is not None
    assert match["figureNumberRaw"] == "9-4"
    assert match["descriptionRaw"].startswith("CIRCUIT CARD ASSEMBLY (A3)")
    # The previous figure's real last entry must be untouched by the merge.
    prev_board = next(e for e in entries if e["partNumberRaw"] == "188C1777P1")
    assert (
        prev_board["descriptionRaw"].strip()
        == ". BOARD, PRINTED WIRING . . . . . . . . . . . . . . ."
    )


def test_mpl_pdf_groups_multi_sheet_figures():
    """A figure captioned '(Sheet 1 of 2)' / '(Sheet 2)' is one logical
    drawing; figureGroups must compose its sheets in order for a structured,
    editable multi-page overlay."""
    result = extract_technical_order(_require_fixture("2JA Test 1.pdf"))
    groups = {g["figureNumber"]: g for g in result["figureGroups"]}

    group = groups["7-1"]
    assert group["composition"] == "multi-sheet"
    assert group["sheetCount"] == 2
    assert group["declaredSheetTotal"] == 2
    assert [s["sheetNumber"] for s in group["sheets"]] == ["1", "2"]
    assert group["figureTitle"] == "Control Unit - Exploded View"


@pytest.mark.parametrize("filename", MPL_FIXTURES)
def test_mpl_pdf_triage_reports_ocr_readiness(filename):
    """Triage must report whether the OCR fallback can actually run, not just
    whether the document looks like it needs it."""
    result = extract_technical_order(_require_fixture(filename))
    triage = result["bom"]["triage"]

    assert "ocrReady" in triage
    assert "bodyMedianCharsPerPage" in triage
    assert triage["sampledPageCount"] >= 6
    # Born-digital documents never run the OCR fallback, so no signal is owed;
    # for every other class an unready OCR fallback must say why.
    if not triage["ocrReady"] and triage["extractionClass"] != "born-digital":
        assert any("OCR" in s for s in triage["signals"])


def test_figure_only_pdf_accepts_bare_captions_and_reports_no_entries():
    result = extract_technical_order(_require_fixture("Test 1 figures.pdf"))

    assert result["entryCount"] == 0
    assert result["figureCount"] == 5
    numbers = sorted(f["figureNumber"] for f in result["figures"])
    assert numbers == ["3-1", "3-2", "3-3", "3-4", "3-5"]
    # Bare captions ("Figure 3-1") carry no title text.
    assert all(f["figureTitle"] == "" for f in result["figures"])


def test_figure_only_pdf_publishes_rendered_media_when_media_dir_given(tmp_path):
    """A drawing-only appendix (no parts table anywhere) must still render
    and publish its figure sheets — zero entries is not the same as zero
    figures, and must not suppress the media / figure manifest."""
    media_dir = tmp_path / "media"
    result = extract_technical_order(
        _require_fixture("Test 1 figures.pdf"), media_dir=media_dir
    )

    assert result["figureCount"] == 5
    media_keys = [f["mediaKey"] for f in result["figures"]]
    assert all(key for key in media_keys)
    for key in media_keys:
        assert (media_dir.parent / key).is_file()

    groups = result["figureGroups"]
    assert len(groups) == 5
    assert all(g["composition"] == "single" for g in groups)


def test_figure_only_pdf_triage_samples_every_page():
    """A 5-page document should sample all 5 pages, not just the first four
    (regression guard for the fraction-rounding gap that used to skip the
    last page on short documents)."""
    result = extract_technical_order(_require_fixture("Test 1 figures.pdf"))
    triage = result["bom"]["triage"]
    assert triage["pageCount"] == 5
    assert triage["sampledPageCount"] == 5


def test_multi_word_usable_on_legend_rows_are_recognized():
    assert _USABLE_ON_LEGEND_ROW.match("A   MODEL 700 SERIES ASSY")


def test_titled_caption_upgrades_earlier_bare_reference():
    _, figures = parse_parts_lists(
        [
            "Figure 3-1",
            "Figure 3-1. Power Supply Block Diagram",
        ]
    )
    assert len(figures) == 1
    assert figures[0].figure_title == "Power Supply Block Diagram"
    assert figures[0].page_number == 2
