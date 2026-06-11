from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "tests" / "prototype" / "out"


def _geometry() -> dict:
    return json.loads((OUT / "pptx-ooxml-geometry.json").read_text())


def _preview_element_style(element_id: str) -> dict[str, float | str]:
    html = (OUT / "preview.html").read_text()
    match = re.search(
        rf'data-element-id="{re.escape(element_id)}"[^>]*style="([^"]+)"',
        html,
    )
    assert match, f"missing preview element {element_id}"
    values: dict[str, float | str] = {}
    for item in match.group(1).split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        value = value.strip()
        if value.endswith("px"):
            values[key.strip()] = float(value[:-2])
        else:
            values[key.strip()] = value
    return values


def test_geometry_json_preserves_all_fixture_slides_without_renderer() -> None:
    document = _geometry()

    assert document["artifactKind"] == "captify.pptx.ooxmlGeometry.v1"
    assert document["source"]["rendererUsed"] is False
    assert document["source"]["conversionUsed"] is False
    assert document["source"]["watermarkRisk"] is False
    assert document["source"]["sha256"]
    assert document["stats"]["slideCount"] == 27
    assert len(document["slides"]) == 27


def test_elements_have_emu_inch_and_canvas_pixel_coordinates() -> None:
    document = _geometry()
    elements = [element for slide in document["slides"] for element in slide["elements"]]

    assert elements
    for element in elements:
        assert set(element["bbox"]) == {"emu", "inches", "px"}
        assert element["bboxSource"]
        assert set(element["bbox"]["emu"]) == {"x", "y", "w", "h"}
        assert set(element["bbox"]["px"]) == {"x", "y", "w", "h"}
        assert all(isinstance(value, int) for value in element["bbox"]["emu"].values())
        assert all(isinstance(value, int | float) for value in element["bbox"]["px"].values())


def test_placeholder_geometry_is_inherited_from_master_when_slide_shape_is_empty() -> None:
    document = _geometry()
    inherited = [
        element
        for slide in document["slides"]
        for element in slide["elements"]
        if element["bboxSource"] == "master_placeholder"
    ]

    assert inherited
    assert all(element["bbox"]["emu"]["w"] > 0 for element in inherited)
    assert all(element["bbox"]["emu"]["h"] > 0 for element in inherited)


def test_geometry_json_keeps_editable_text_media_and_notes() -> None:
    document = _geometry()

    assert document["stats"]["textElementCount"] > 0
    assert document["stats"]["imageElementCount"] > 0
    assert document["stats"]["masterDecorationElementCount"] > 0
    assert document["stats"]["ooxmlNotesSlideCount"] > 0
    assert document["assets"]
    assert any(
        element["text"]["paragraphs"]
        for slide in document["slides"]
        for element in slide["elements"]
        if element["type"] == "text"
    )
    assert any(
        element["text"]["runs"]
        for slide in document["slides"]
        for element in slide["elements"]
        if element["type"] == "text"
    )


def test_table_elements_keep_bbox_and_structured_cells() -> None:
    document = _geometry()
    tables = [
        element
        for slide in document["slides"]
        for element in slide["elements"]
        if element["type"] == "table"
    ]

    assert tables
    assert all(element["bbox"]["px"]["w"] > 0 for element in tables)
    assert all(element["bbox"]["px"]["h"] > 0 for element in tables)
    assert all((element["text"].get("rows") or []) for element in tables)
    assert all((element["text"].get("table") or {}).get("styleId") for element in tables)
    assert all((element["text"].get("table") or {}).get("styleDefinition") for element in tables)
    assert all((element["text"].get("table") or {}).get("columns") for element in tables)
    assert all((element["text"].get("table") or {}).get("rows") for element in tables)
    assert all(
        cell.get("paragraphs")
        for element in tables
        for row in element["text"]["table"]["rows"]
        for cell in row["cells"]
    )
    assert any(
        cell["paragraphs"][0]["runs"][0]["font"]["size"] == 12.0
        for element in tables
        for row in element["text"]["table"]["rows"]
        for cell in row["cells"]
        if cell.get("paragraphs") and cell["paragraphs"][0].get("runs")
    )
    assert any(
        (((element["text"]["table"]["styleDefinition"]["parts"].get("firstRow") or {}).get("fill") or {}).get("color") or {}).get("value")
        for element in tables
    )
    assert all(
        cell.get("effectiveStyle")
        for element in tables
        for row in element["text"]["table"]["rows"]
        for cell in row["cells"]
    )
    assert any(
        cell["effectiveStyle"]["text"].get("bold") == "on"
        and ((cell["effectiveStyle"]["text"].get("color") or {}).get("value") == "#FFFFFF")
        and "firstRow" in cell["effectiveStyle"].get("appliedParts", [])
        for element in tables
        for cell in element["text"]["table"]["rows"][0]["cells"]
    )
    assert any(
        ((cell["effectiveStyle"].get("fill") or {}).get("color") or {}).get("value")
        for element in tables
        for row in element["text"]["table"]["rows"][1:]
        for cell in row["cells"]
    )


def test_geometry_json_includes_bloom_instructional_metadata() -> None:
    document = _geometry()
    valid_levels = {"Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"}

    bloom_entries = [
        slide["instructionalMetadata"]["bloom"]
        for slide in document["slides"]
    ]

    assert len(bloom_entries) == 27
    assert all(entry["taxonomy"] == "Bloom" for entry in bloom_entries)
    assert all(entry["primaryLevel"] in valid_levels for entry in bloom_entries)
    assert all(entry["method"] == "deterministic_verb_heuristic" for entry in bloom_entries)
    assert all(entry["status"] == "needs_llm_review" for entry in bloom_entries)


def test_title_line_breaks_and_run_fonts_are_preserved() -> None:
    document = _geometry()
    slide = document["slides"][11]
    title = next(
        element
        for element in slide["elements"]
        if element["type"] == "text" and element["source"]["placeholderType"] == "title"
    )
    runs = title["text"]["paragraphs"][0]["runs"]

    assert title["text"]["plain"].startswith("\nAFTO FORM 874\nPart B")
    assert [run["text"] for run in runs[:4]] == [
        "\n",
        "AFTO FORM 874",
        "\n",
        "Part B - Action Required On Spares",
    ]
    assert runs[1]["font"]["family"] == "Times New Roman"
    assert runs[1]["font"]["size"] == 28.0
    assert runs[3]["font"]["family"] == "Times New Roman"
    assert runs[3]["font"]["size"] == 24.0
    assert runs[3]["font"]["underline"] == "single"


def test_inherited_bullets_are_preserved_for_body_text() -> None:
    document = _geometry()
    slide = document["slides"][1]
    body = next(element for element in slide["elements"] if element["elementId"] == "slide-002-block-002")
    paragraphs = [
        paragraph
        for paragraph in body["text"]["paragraphs"]
        if "".join(run.get("text", "") for run in paragraph.get("runs", [])).strip()
    ]

    assert body["text"]["plain"].count("•") >= 3
    assert (paragraphs[0].get("bullet") or {}).get("kind") in {None, "none"}
    assert all((paragraph.get("bullet") or {}).get("char") == "•" for paragraph in paragraphs[1:4])
    assert any(paragraph.get("marginLeftEmu") is not None for paragraph in paragraphs[1:4])
    assert any(paragraph.get("spacingBefore") for paragraph in body["text"]["paragraphs"])
    assert any(paragraph.get("spacingAfter") for paragraph in body["text"]["paragraphs"])
    assert any(paragraph.get("empty") for paragraph in body["text"]["paragraphs"])
    assert "line-height:normal" in (OUT / "preview.html").read_text()
    assert "&nbsp;" in (OUT / "preview.html").read_text()


def test_slide_format_exposes_layout_master_theme_and_title_lines() -> None:
    document = _geometry()
    slide = document["slides"][11]
    slide_format = slide["slideFormat"]

    assert slide_format["layoutName"] == "Title and Content"
    assert slide_format["slideLayoutPart"].startswith("ppt/slideLayouts/")
    assert slide_format["slideMasterPart"].startswith("ppt/slideMasters/")
    assert slide_format["themeFile"] == "ppt/theme/theme1.xml"
    assert slide_format["formatSources"]["slide"] == "ppt/slides/slide12.xml"
    assert slide_format["titleStructure"]["lines"] == [
        "AFTO FORM 874",
        "Part B - Action Required On Spares",
    ]


def test_image_assets_include_context_slots_for_llm_captions() -> None:
    document = _geometry()
    image_assets = [asset for asset in document["assets"] if asset["kind"] == "image"]
    content_image_asset_ids = {
        element["assetId"]
        for slide in document["slides"]
        for element in slide["elements"]
        if element["type"] == "image" and element["kind"] != "master_image"
    }

    assert image_assets
    assert content_image_asset_ids
    assert document["stats"]["imageContext"]["imageAssets"] == len(content_image_asset_ids)
    for asset in image_assets:
        if asset["assetId"] not in content_image_asset_ids:
            assert asset["imageContext"] is None
            continue
        context = asset["imageContext"]
        assert context["provider"]
        assert "text" in context
        assert context.get("text") or context.get("reason")


def test_embedded_content_images_get_ocr_and_grid_extraction() -> None:
    document = _geometry()
    slide = document["slides"][13]
    image = next(
        element
        for element in slide["elements"]
        if element["type"] == "image" and element["kind"] != "master_image"
    )
    extraction = image["imageExtraction"]

    assert extraction["method"] == "tesseract_ocr_plus_opencv_grid"
    assert "KIT/PARTS REQUIRED TO MODIFY SPARES" in extraction["text"]
    assert "PART C." in extraction["lines"]
    assert extraction["wordCount"] >= 40
    assert extraction["averageConfidence"] > 0.5
    assert extraction["grid"]["tableLike"] is True
    assert extraction["grid"]["horizontalLineCount"] > 0
    assert extraction["grid"]["verticalLineCount"] > 0
    assert extraction["words"][0]["bbox"]["relative"]["w"] > 0
    assert document["stats"]["imageContext"]["embeddedImageOcrAssets"] > 0
    assert document["stats"]["imageContext"]["embeddedImageGridAssets"] > 0
    assert "Embedded image extraction" in (OUT / "preview.html").read_text()
    assert "KIT/PARTS REQUIRED TO MODIFY SPARES" in (OUT / "preview.html").read_text()


def test_text_plus_image_slides_render_text_above_content_images() -> None:
    document = _geometry()
    for slide_index in range(13, 21):
        slide = document["slides"][slide_index]
        body = next(element for element in slide["elements"] if element["elementId"].endswith("-block-002"))
        image = next(
            element
            for element in slide["elements"]
            if element["type"] == "image" and element["kind"] != "master_image"
        )
        style = _preview_element_style(body["elementId"])
        expected_height = round(image["bbox"]["px"]["y"] - body["bbox"]["px"]["y"] - 10, 2)

        assert style["height"] == expected_height
        assert style["padding-right"] == 4.0
        assert style["width"] > 900


def test_inherited_master_decorations_are_canvas_elements() -> None:
    document = _geometry()
    master_elements = [
        element
        for slide in document["slides"]
        for element in slide["elements"]
        if element["bboxSource"] == "slide_master"
    ]

    assert master_elements
    assert any(element["kind"] == "master_image" for element in master_elements)
    assert any(element["kind"] == "master_shape" for element in master_elements)
    assert all(element["canvas"]["layer"] == "master_decoration" for element in master_elements)
    assert all(element["editable"] is False for element in master_elements)
    assert all(element["bbox"]["emu"]["w"] > 0 for element in master_elements)
    assert all(element["bbox"]["emu"]["h"] > 0 for element in master_elements)


def test_extracted_xml_parts_are_indexed() -> None:
    document = _geometry()
    parts = {part["sourcePart"] for part in document["xmlParts"]}
    categories = {part["category"] for part in document["xmlParts"]}

    assert "ppt/presentation.xml" in parts
    assert "ppt/slides/slide1.xml" in parts
    assert "ppt/slides/slide27.xml" in parts
    assert "ppt/slides/_rels/slide1.xml.rels" in parts
    assert "slideLayout" in categories
    assert "slideMaster" in categories
    assert "notesSlide" in categories
    assert all(part["sha256"] for part in document["xmlParts"])


def test_canvas_contract_is_file_type_neutral_and_shape_based() -> None:
    contract = json.loads((OUT / "canvas-contract.json").read_text())

    assert contract["contractKind"] == "captify.canvas.deepDocument.v1"
    assert contract["source"]["rendererUsed"] is False
    assert len(contract["units"]) == 27
    assert contract["layers"] == [
        "slide_frame",
        "background",
        "master_decoration",
        "source_asset",
        "editable_content",
        "structural_placeholder",
        "quality_overlay",
    ]
    assert any(shape["editable"] for unit in contract["units"] for shape in unit["shapes"])


def test_tldraw_proof_uses_ooxml_shapes_and_assets_not_rendered_pages() -> None:
    tldr = json.loads((OUT / "pptx-ooxml-geometry.tldr").read_text())
    shapes = [record for record in tldr["records"] if record.get("typeName") == "shape"]
    assets = [record for record in tldr["records"] if record.get("typeName") == "asset"]

    assert tldr["tldrawFileFormatVersion"] == 1
    assert shapes
    assert assets
    assert not any(
        shape.get("meta", {}).get("role") == "locked_visual_reference" for shape in shapes
    )
    assert any(shape.get("meta", {}).get("role") == "slide_frame" for shape in shapes)


def test_preview_html_is_generated_for_visual_inspection() -> None:
    preview = OUT / "preview.html"
    preview_text = preview.read_text()

    assert preview.exists()
    assert "Prototype OOXML Geometry Preview" in preview_text
    assert "Speaker Notes" in preview_text
    assert "Image Context" in preview_text
    assert "Bloom Taxonomy" in preview_text
    assert "Original Slide PNG (PDF Reference)" in preview_text
    assert "Extracted Slide Render" in preview_text
    assert "Slide JSON Object" in preview_text
    assert "extraction-comparison-summary.json" in preview_text
    assert "extracted-table" in preview_text
    assert "json-key" in preview_text
    assert "review-panel" in preview_text
    assert "master-image" in preview_text
    assert "<br>" in preview_text
    assert (OUT / "slide-png" / "slide-001.png").exists()
    assert (OUT / "pdf-reference-map.json").exists()
    assert (OUT / "extraction-comparison-summary.json").exists()


def test_extraction_comparison_covers_every_slide_against_pdf_reference() -> None:
    comparison = json.loads((OUT / "extraction-comparison-summary.json").read_text())

    assert comparison["artifactKind"] == "captify.extractionComparison.v1"
    assert comparison["summary"]["slideCount"] == 27
    assert comparison["summary"]["matchedSlideCount"] == 27
    assert len(comparison["slides"]) == 27
    assert all(record["pdfPageNumber"] for record in comparison["slides"])
