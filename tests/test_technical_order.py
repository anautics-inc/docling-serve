"""Technical Order extractor and MPL parser tests (no live PDF required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docling_serve.extractors import ExtractionContext, select_extractor
from docling_serve.extractors.technical_order.metadata import parse_to_metadata
from docling_serve.extractors.technical_order.mpl import (
    _normalize_cage_and_description,
    parse_parts_lists,
)
from docling_serve.extractors.technical_order.triage import triage_pdf
from docling_serve.extractors.technical_order_extractor import (
    TechnicalOrderExtractor,
    _looks_like_technical_order,
)

FIXTURES = Path(__file__).parent / "test_files" / "technical_order"
SAMPLE_PDF = Path("/tmp/to-review/Non CUI TOs/Post 2014/34Y3-30-1.pdf")


def _ctx(tmp_path: Path, name: str, *, profile: str = "default") -> ExtractionContext:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    return ExtractionContext(
        source_path=tmp_path / name,
        bundle_dir=bundle,
        media_dir=bundle / "media",
        source_manifest_key=f"task:test:{Path(name).stem}",
        task_id="test",
        profile=profile,
    )


@pytest.mark.parametrize(
    ("cage", "desc", "expected_cage", "expected_desc"),
    [
        ("80205   .", ". NUT, Plain", "80205", ". . NUT, Plain"),
        ("00ET4   .", "BRACKET, Cover", "00ET4", ". BRACKET, Cover"),
        ("80205", ". WASHER, Lock", "80205", ". WASHER, Lock"),
        ("00ET4", ". BASKET", "00ET4", ". BASKET"),
    ],
)
def test_normalize_cage_and_description(cage, desc, expected_cage, expected_desc):
    out_cage, out_desc = _normalize_cage_and_description(cage, desc)
    assert out_cage == expected_cage
    assert out_desc == expected_desc


def test_parse_mpl_pages_matches_golden():
    pages = json.loads((FIXTURES / "34Y3-30-1-mpl-pages.json").read_text())
    golden = json.loads((FIXTURES / "34Y3-30-1-mpl-golden.json").read_text())
    entries, figures = parse_parts_lists(pages, first_page_number=7)

    assert len(figures) == len(golden["figures"])
    assert figures[0].figure_number == golden["figures"][0]["figureNumber"]

    by_seq = {e.sequence: e.as_dict() for e in entries}
    for expected in golden["entries"]:
        got = by_seq[expected["sequence"]]
        for key in (
            "pageNumber",
            "figureIndexRaw",
            "partNumberRaw",
            "cageRaw",
            "indentureLevel",
            "parentSequence",
            "rowType",
        ):
            assert got[key] == expected[key], f"seq {expected['sequence']} {key}"
        assert got["descriptionRaw"].startswith(
            expected["descriptionRaw"][:20].rstrip(". ")
        )


def test_parse_title_pages_metadata():
    pages = json.loads((FIXTURES / "34Y3-30-1-pages.json").read_text())[:3]
    meta = parse_to_metadata(pages, filename="34Y3-30-1.pdf")
    assert meta.document_number == "34Y3-30-1"
    assert "DEGREASER" in meta.document_title.upper()
    assert meta.end_item_part_number_raw == "51079"
    assert "4940" in meta.end_item_nsn_raw.replace(" ", "")


def test_chapter_dash_figures_and_index_tokens():
    """Modern MPLs use 'Figure 6-2.' captions and '6-1-' index tokens."""
    page = "\n".join(
        [
            "             Figure 6-1.   LOX Storage Tank, 3000 Gallon",
            "FIGURE &                                              UNITS   USABLE",
            " INDEX/         PART              DESCRIPTION         PER      ON     SMR",
            "SHEET NO.      NUMBER    CAGE    1234567              ASSY     CODE    CODE",
            "6-1-        CVA-3K-SK    4MKL5   TANK ASSEMBLY, LOX     1               XB",
            "        1   CVA59C66     4MKL5   . HOSE ASSEMBLY        1               XB",
            "        2   8991174      4MKL5   . . FITTING, Female    2               XB",
        ]
    )
    entries, figures = parse_parts_lists([page])
    assert [f.figure_number for f in figures] == ["6-1"]
    assert [e.figure_number_raw for e in entries] == ["6-1", "6-1", "6-1"]
    assert [e.figure_index_raw for e in entries] == ["", "1", "2"]
    assert entries[0].row_type == "end-item"


def test_figure_caption_sheet_without_total():
    page = "Figure 4-2.   Controls and Indicators (Sheet 2)\n"
    _, figures = parse_parts_lists([page])
    assert figures[0].figure_number == "4-2"
    assert figures[0].sheet_number == "2"
    assert figures[0].sheet_total is None


def test_title_nomenclature_when_headline_wraps_lines():
    """Headline wrapped across lines + level qualifier must not eat the title."""
    page = "\n".join(
        [
            "TO 37C2-8-38-1",
            "TECHNICAL MANUAL",
            "",
            "OPERATION AND MAINTENANCE INSTRUCTIONS",
            "WITH",
            "ILLUSTRATED PARTS BREAKDOWN",
            "",
            "INTERMEDIATE LEVEL",
            "",
            "STORAGE VESSEL, LIQUID OXYGEN,",
            "3000 GALLON",
            "",
            "PN CVA-3.0K-60-SK-AF-LOX",
            "NSN 3655-01-552-3894",
        ]
    )
    meta = parse_to_metadata([page], filename="37C2-8-38-1.pdf")
    assert meta.document_title == "STORAGE VESSEL, LIQUID OXYGEN, 3000 GALLON"
    assert meta.end_item_part_number_raw == "CVA-3.0K-60-SK-AF-LOX"


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("technical-order", "extract_technical_order"),
        ("to-ipb", "extract_technical_order"),
        ("schematic", "extract_schematic"),
    ],
)
def test_select_extractor_profile(tmp_path, profile, expected):
    assert select_extractor(_ctx(tmp_path, "manual.pdf", profile=profile)).name == expected


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="sample TO PDF not extracted")
def test_figure_hotspots_detect_callouts(tmp_path):
    import subprocess

    from docling_serve.extractors.technical_order.figure_hotspots import (
        detect_figure_hotspots,
    )
    from docling_serve.extractors.technical_order import page_layout_texts

    entries, figures = parse_parts_lists(page_layout_texts(SAMPLE_PDF))
    figure = figures[0]
    prefix = tmp_path / "sheet"
    subprocess.run(
        ["pdftoppm", "-png", "-r", "150", "-f", str(figure.page_number),
         "-l", str(figure.page_number), "-singlefile", str(SAMPLE_PDF), str(prefix)],
        check=True,
    )
    valid = {e.figure_index_raw for e in entries if e.figure_index_raw}
    hotspots = detect_figure_hotspots(prefix.with_suffix(".png"), valid)
    assert len(hotspots) >= 10, "exploded view carries many callouts"
    for spot in hotspots:
        assert spot.index in valid
        assert 0 <= spot.x0 < spot.x1 <= 1
        assert 0 <= spot.y0 < spot.y1 <= 1
        assert spot.confidence >= 60


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="sample TO PDF not extracted")
def test_row_boxes_attach_to_all_entries():
    from docling_serve.extractors.technical_order import page_layout_texts
    from docling_serve.extractors.technical_order.rowbox import attach_row_boxes

    entries, _ = parse_parts_lists(page_layout_texts(SAMPLE_PDF))
    matched = attach_row_boxes(entries, SAMPLE_PDF)
    assert matched == len(entries)
    box = entries[0].row_box
    assert box is not None
    x0, y0, x1, y1 = box
    assert 0 <= x0 < x1 <= 1
    assert 0 <= y0 < y1 <= 1
    assert entries[0].as_dict()["rowBox"] == list(box)


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="sample TO PDF not extracted")
def test_triage_sample_to():
    triage = triage_pdf(SAMPLE_PDF)
    assert triage.extraction_class == "born-digital"
    assert triage.format_family == "mpl-modern"
    assert triage.document_type == "TO-IPB"
    assert triage.document_kind == "basic"


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="sample TO PDF not extracted")
def test_auto_profile_detects_to():
    assert _looks_like_technical_order(SAMPLE_PDF) is True


@pytest.mark.skipif(not SAMPLE_PDF.is_file(), reason="sample TO PDF not extracted")
def test_extractor_builds_bom_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    media = bundle / "media"
    media.mkdir()
    ctx = ExtractionContext(
        source_path=SAMPLE_PDF,
        bundle_dir=bundle,
        media_dir=media,
        source_manifest_key="task:test:34Y3-30-1",
        task_id="test",
        profile="technical-order",
    )
    result = TechnicalOrderExtractor().build(ctx)
    bom = json.loads((bundle / "bom.json").read_text())

    assert result.extractor == "extract_technical_order"
    assert result.domain == "technical-order"
    assert bom["schema"] == "captify.bom.v1"
    assert bom["stats"]["entryCount"] == 87
    assert bom["document"]["documentNumber"] == "34Y3-30-1"
    assert (bundle / "bom.json").is_file()
    assert result.manifest_extra["technicalOrder"]["bom"] == "bom.json"
