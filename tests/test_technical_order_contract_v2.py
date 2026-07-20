"""Focused contract tests for Technical Order v2 identity and provenance."""

from __future__ import annotations

from docling_serve.technical_order.bundle import build_bom_payload
from docling_serve.technical_order.content import parse_content
from docling_serve.technical_order.metadata import TOMetadata
from docling_serve.technical_order.mpl import FigureRecord, PartsListEntry
from docling_serve.technical_order.triage import TriageResult


def _triage() -> TriageResult:
    return TriageResult(
        extraction_class="born-digital",
        format_family="mpl-modern",
        document_kind="basic",
        document_type="TO-IPB",
        page_count=2,
    )


def _entities() -> tuple[list[PartsListEntry], list[FigureRecord]]:
    entries = [
        PartsListEntry(
            sequence=1,
            page_number=2,
            figure_number_raw="1-1",
            figure_index_raw="1",
            part_number_raw="RAW-001",
            description_raw=". BRACKET, RAW",
            row_box=(0.1, 0.2, 0.8, 0.3),
        ),
        PartsListEntry(
            sequence=2,
            page_number=2,
            figure_number_raw="1-1",
            figure_index_raw="2",
            part_number_raw="RAW-002",
            description_raw=". . BOLT, RAW",
            parent_sequence=1,
        ),
    ]
    figures = [
        FigureRecord(
            figure_number="1-1",
            sheet_number="1",
            page_number=1,
            hotspots=[
                {
                    "index": "1",
                    "box": [0.2, 0.3, 0.25, 0.35],
                    "confidence": 91.0,
                    "provenance": {
                        "method": "tesseract-ocr",
                        "parser": {"name": "test", "version": "2"},
                        "confidence": 0.91,
                        "sourceGeometry": {
                            "coordinateSystem": "normalized-page-top-left",
                            "boundingBox": [0.2, 0.3, 0.25, 0.35],
                        },
                    },
                }
            ],
        )
    ]
    return entries, figures


def test_bom_v2_ids_provenance_markings_and_raw_compatibility(tmp_path):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"stable source bytes")
    metadata = TOMetadata(
        document_number="TO-1",
        distribution_statement="DISTRIBUTION STATEMENT C. Test restriction.",
    )
    entries, figures = _entities()

    first = build_bom_payload(
        pdf_path=pdf,
        triage=_triage(),
        metadata=metadata,
        entries=entries,
        figures=figures,
    )
    second_entries, second_figures = _entities()
    second = build_bom_payload(
        pdf_path=pdf,
        triage=_triage(),
        metadata=metadata,
        entries=second_entries,
        figures=second_figures,
    )

    assert first["schema"] == "captify.bom.v2"
    assert first["compatibleSchemas"] == ["captify.bom.v1"]
    assert first["publication"] == {
        "publicationId": first["publication"]["publicationId"],
        "documentNumber": "TO-1",
        "title": "",
        "publicationType": "TO-IPB",
    }
    assert first["publicationIssue"] == {
        "issueId": first["id"],
        "revision": "basic",
        "issueKind": "basic",
        "publicationDate": None,
    }
    assert first["id"] == second["id"]
    assert (
        first["publication"]["publicationId"] == second["publication"]["publicationId"]
    )
    assert [row["id"] for row in first["entries"]] == [
        row["id"] for row in second["entries"]
    ]
    assert first["entries"][1]["parentId"] == first["entries"][0]["id"]
    assert first["entries"][0]["descriptionRaw"] == ". BRACKET, RAW"
    assert first["entries"][0]["provenance"]["sourceGeometry"]["boundingBox"] == [
        0.1,
        0.2,
        0.8,
        0.3,
    ]
    figure = first["figures"][0]
    assert figure["id"] == second["figures"][0]["id"]
    assert figure["hotspots"][0]["figureSheetId"] == figure["id"]
    assert figure["hotspots"][0]["confidence"] == 0.91
    assert figure["hotspots"][0]["provenance"]["sourceGeometry"]["pageNumber"] == 1
    assert first["entries"][0]["markings"]["distributionStatement"].startswith(
        "DISTRIBUTION"
    )


def test_content_v2_stable_page_and_block_ids_with_marking_propagation():
    kwargs = {
        "document_id": "technical-order_fixed",
        "markings": {"distributionStatement": "DISTRIBUTION STATEMENT A."},
    }
    first = parse_content(["TITLE", "1.1 Paragraph text."], **kwargs)
    second = parse_content(["TITLE", "1.1 Paragraph text."], **kwargs)

    assert first["schema"] == "captify.to.v2"
    assert [page["id"] for page in first["pages"]] == [
        page["id"] for page in second["pages"]
    ]
    assert first["pages"][1]["blocks"][0]["id"] == second["pages"][1]["blocks"][0]["id"]
    assert (
        first["pages"][1]["blocks"][0]["markings"]["inheritedFrom"]
        == first["pages"][1]["id"]
    )
