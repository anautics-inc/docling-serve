from __future__ import annotations

from docling_serve.deep_document.docling_adapter import manifest_from_docling_document
from docling_serve.deep_document.document_builder import build_deep_document


def test_pdf_uses_page_units_from_docling_pages() -> None:
    manifest = manifest_from_docling_document(
        {
            "pages": {"1": {"page_no": 1, "size": {"width": 612, "height": 792}}},
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "title",
                    "text": "Policy",
                    "prov": [{"page_no": 1}],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        },
        filename="policy.pdf",
        source_manifest_key="task:t1:policy",
    )

    deep_document = build_deep_document(
        manifest=manifest,
        source_manifest_key="task:t1:policy",
    )

    unit = deep_document["document"]["units"][0]
    assert deep_document["source"]["fileKind"] == "pdf"
    assert deep_document["document"]["unitType"] == "page"
    assert unit["sourceRefs"]["pageNo"] == 1
    assert unit["render"]["size"]["px"] == {"width": 612.0, "height": 792.0}


def test_pptx_uses_slide_units_from_docling_pages() -> None:
    manifest = manifest_from_docling_document(
        {
            "pages": {"1": {"page_no": 1, "size": {"width": 960, "height": 540}}},
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "title",
                    "text": "Training Slide",
                    "prov": [{"page_no": 1}],
                }
            ],
            "body": {"children": [{"$ref": "#/texts/0"}]},
        },
        filename="training.pptx",
        source_manifest_key="task:t1:training",
    )

    deep_document = build_deep_document(
        manifest=manifest,
        source_manifest_key="task:t1:training",
    )

    assert deep_document["source"]["fileKind"] == "presentation"
    assert deep_document["document"]["unitType"] == "slide"


def test_docx_uses_section_units_from_headings() -> None:
    manifest = manifest_from_docling_document(
        {
            "texts": [
                {"self_ref": "#/texts/0", "label": "section_header", "text": "Purpose"},
                {"self_ref": "#/texts/1", "label": "text", "text": "Do the work."},
                {"self_ref": "#/texts/2", "label": "section_header", "text": "Scope"},
                {"self_ref": "#/texts/3", "label": "text", "text": "Applies to teams."},
            ],
            "body": {
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/texts/1"},
                    {"$ref": "#/texts/2"},
                    {"$ref": "#/texts/3"},
                ]
            },
        },
        filename="procedure.docx",
        source_manifest_key="task:t1:procedure",
    )

    deep_document = build_deep_document(
        manifest=manifest,
        source_manifest_key="task:t1:procedure",
    )

    assert deep_document["source"]["fileKind"] == "word"
    assert deep_document["document"]["unitType"] == "section"
    assert [unit["title"] for unit in deep_document["document"]["units"]] == [
        "Purpose",
        "Scope",
    ]


def test_xlsx_uses_sheet_units_and_preserves_table_cells() -> None:
    manifest = manifest_from_docling_document(
        {
            "sheets": [{"name": "Overview"}, {"name": "Scores"}],
            "tables": [
                {
                    "self_ref": "#/tables/0",
                    "sheetName": "Scores",
                    "data": {
                        "table_cells": [
                            {
                                "text": "Total",
                                "start_row_offset_idx": 0,
                                "start_col_offset_idx": 0,
                            }
                        ]
                    },
                }
            ],
            "body": {"children": [{"$ref": "#/tables/0"}]},
        },
        filename="training.xlsx",
        source_manifest_key="task:t1:training",
    )

    deep_document = build_deep_document(
        manifest=manifest,
        source_manifest_key="task:t1:training",
    )

    assert deep_document["source"]["fileKind"] == "spreadsheet"
    assert deep_document["document"]["unitType"] == "sheet"
    assert [unit["title"] for unit in deep_document["document"]["units"]] == [
        "Overview",
        "Scores",
    ]
    scores = deep_document["document"]["units"][1]
    assert scores["sourceRefs"]["sheetName"] == "Scores"
    assert scores["content"]["tables"][0]["text"]["plain"] == "Total"


def test_image_upload_uses_image_unit() -> None:
    manifest = manifest_from_docling_document(
        {
            "pages": {"1": {"page_no": 1, "size": {"width": 640, "height": 480}}},
            "pictures": [
                {
                    "self_ref": "#/pictures/0",
                    "label": "picture",
                    "text": "Detected diagram",
                    "prov": [{"page_no": 1}],
                }
            ],
            "body": {"children": [{"$ref": "#/pictures/0"}]},
        },
        filename="diagram.png",
        source_manifest_key="task:t1:diagram",
    )

    deep_document = build_deep_document(
        manifest=manifest,
        source_manifest_key="task:t1:diagram",
    )

    assert deep_document["source"]["fileKind"] == "image"
    assert deep_document["document"]["unitType"] == "image"
    assert deep_document["document"]["units"][0]["content"]["images"]
