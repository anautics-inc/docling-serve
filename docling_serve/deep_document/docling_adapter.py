from __future__ import annotations

from pathlib import Path
from typing import Any

CONTENT_COLLECTIONS = ("texts", "tables", "pictures")
TITLE_LABELS = frozenset({"title", "section_header", "page_header"})
PRESENTATION_EXTENSIONS = {".ppt", ".pptx"}
SPREADSHEET_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".csv"}
WORD_EXTENSIONS = {".doc", ".docx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"}


def manifest_from_docling_document(
    doc: dict[str, Any],
    *,
    filename: str | None,
    source_manifest_key: str,
) -> dict[str, Any]:
    kind = source_kind(filename)
    return {
        "artifactKind": "captify.doclingDocument.normalizedExtraction.v1",
        "source": {
            "originalFileName": filename,
            "fileKind": kind,
            "sha256": "",
            "rendererUsed": False,
            "conversionUsed": False,
            "watermarkRisk": False,
            "sourceManifestKey": source_manifest_key,
        },
        "units": parse_docling_units(doc, kind=kind),
        "assets": [],
    }


def parse_docling_units(
    doc: dict[str, Any], *, kind: str = "document"
) -> list[dict[str, Any]]:
    page_sizes = docling_page_sizes(doc)
    items = collect_items(doc)
    if kind == "spreadsheet":
        return spreadsheet_units(items, doc)
    if page_sizes:
        unit_type = (
            "slide"
            if kind == "presentation"
            else "image"
            if kind == "image"
            else "page"
        )
        return page_units(items, page_sizes, unit_type=unit_type)
    if kind == "image":
        return image_units(items)
    return section_units(items)


def source_kind(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in PRESENTATION_EXTENSIONS:
        return "presentation"
    if suffix in SPREADSHEET_EXTENSIONS:
        return "spreadsheet"
    if suffix in WORD_EXTENSIONS:
        return "word"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    return "document"


def docling_page_sizes(doc: dict[str, Any]) -> dict[int, dict[str, float]]:
    pages = doc.get("pages") or {}
    result = {}
    for key, page in pages.items():
        try:
            page_no = int(page.get("page_no", key))
        except (TypeError, ValueError):
            continue
        size = page.get("size") or {}
        result[page_no] = {
            "width": float(size.get("width", 0.0)),
            "height": float(size.get("height", 0.0)),
        }
    return result


def collect_items(doc: dict[str, Any]) -> list[dict[str, Any]]:
    order = reading_order(doc)
    items = []
    for collection in CONTENT_COLLECTIONS:
        for index, item in enumerate(doc.get(collection, []) or []):
            if not isinstance(item, dict):
                continue
            text = item_text(item, collection)
            items.append(
                {
                    "kind": "table"
                    if collection == "tables"
                    else "picture"
                    if collection == "pictures"
                    else "text",
                    "doclingLabel": item.get("label"),
                    "text": text,
                    "pageNo": item_page(item),
                    "sheetName": item_sheet_name(item),
                    "readingOrder": order.get(item.get("self_ref", ""), index),
                    "table": table_payload(item) if collection == "tables" else None,
                }
            )
    return items


def reading_order(doc: dict[str, Any]) -> dict[str, int]:
    order = {}
    counter = 0
    ref_index = {}
    for collection in (*CONTENT_COLLECTIONS, "groups"):
        for item in doc.get(collection, []) or []:
            if isinstance(item, dict) and item.get("self_ref"):
                ref_index[item["self_ref"]] = item

    def visit(ref: str) -> None:
        nonlocal counter
        if ref in order:
            return
        item = ref_index.get(ref)
        if item is None:
            return
        if ref.startswith("#/groups/"):
            for child in item.get("children", []) or []:
                child_ref = child.get("$ref") if isinstance(child, dict) else None
                if child_ref:
                    visit(child_ref)
            return
        order[ref] = counter
        counter += 1

    for child in (doc.get("body") or {}).get("children", []) or []:
        child_ref = child.get("$ref") if isinstance(child, dict) else None
        if child_ref:
            visit(child_ref)
    return order


def item_page(item: dict[str, Any]) -> int | None:
    prov = item.get("prov")
    if isinstance(prov, list) and prov and prov[0].get("page_no") is not None:
        return int(prov[0]["page_no"])
    return None


def item_sheet_name(item: dict[str, Any]) -> str | None:
    for key in ("sheet_name", "sheetName", "sheet"):
        if item.get(key):
            return str(item[key])
    data = item.get("data") or {}
    for key in ("sheet_name", "sheetName", "sheet"):
        if data.get(key):
            return str(data[key])
    prov = item.get("prov")
    if isinstance(prov, list) and prov:
        first = prov[0]
        if isinstance(first, dict):
            for key in ("sheet_name", "sheetName", "sheet"):
                if first.get(key):
                    return str(first[key])
    return None


def item_text(item: dict[str, Any], collection: str) -> str:
    if collection == "tables":
        cells = table_payload(item).get("cells", [])
        return "\n".join(
            str(cell.get("text") or "") for cell in cells if cell.get("text")
        )
    return str(item.get("text") or item.get("orig") or "").strip()


def table_payload(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") or {}
    cells = [
        {
            "text": cell.get("text", ""),
            "rowIndex": cell.get("start_row_offset_idx"),
            "colIndex": cell.get("start_col_offset_idx"),
        }
        for cell in data.get("table_cells", []) or []
    ]
    return {"cells": cells}


def page_units(
    items: list[dict[str, Any]],
    page_sizes: dict[int, dict[str, float]],
    *,
    unit_type: str = "page",
) -> list[dict[str, Any]]:
    units = []
    for index, page_no in enumerate(sorted(page_sizes)):
        page_items = sorted(
            [item for item in items if item.get("pageNo") == page_no],
            key=lambda item: item["readingOrder"],
        )
        unit = finalize_unit(index, unit_type, page_items, forced_title=None)
        unit["render"]["size"] = {"px": page_sizes[page_no]}
        unit["sourceRefs"]["pageNo"] = page_no
        units.append(unit)
    return units


def spreadsheet_units(
    items: list[dict[str, Any]], doc: dict[str, Any]
) -> list[dict[str, Any]]:
    sheet_names = docling_sheet_names(doc)
    if not sheet_names:
        sheet_names = sorted(
            {item["sheetName"] for item in items if item.get("sheetName")}
        )
    if not sheet_names:
        sheet_names = ["Sheet 1"]

    units = []
    for index, sheet_name in enumerate(sheet_names):
        sheet_items = [
            item
            for item in items
            if (item.get("sheetName") or ("Sheet 1" if len(sheet_names) == 1 else None))
            == sheet_name
        ]
        if not sheet_items and len(sheet_names) == 1:
            sheet_items = items
        unit = finalize_unit(index, "sheet", sheet_items, forced_title=sheet_name)
        unit["sourceRefs"]["sheetName"] = sheet_name
        units.append(unit)
    return units


def docling_sheet_names(doc: dict[str, Any]) -> list[str]:
    candidates = []
    for value in (
        doc.get("sheets"),
        (doc.get("workbook") or {}).get("sheets")
        if isinstance(doc.get("workbook"), dict)
        else None,
    ):
        if isinstance(value, dict):
            candidates.extend(
                str(sheet.get("name") or key) for key, sheet in value.items()
            )
        elif isinstance(value, list):
            for sheet in value:
                if isinstance(sheet, dict):
                    candidates.append(
                        str(sheet.get("name") or sheet.get("sheetName") or "")
                    )
                elif sheet:
                    candidates.append(str(sheet))
    return [name for name in candidates if name.strip()]


def image_units(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [finalize_unit(0, "image", items, forced_title="Image")]


def section_units(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: item["readingOrder"])
    sections: list[tuple[str | None, list[dict[str, Any]]]] = []
    current_title = None
    current: list[dict[str, Any]] = []
    for item in ordered:
        is_header = (
            item["kind"] == "text"
            and item.get("doclingLabel") in TITLE_LABELS
            and item.get("text")
        )
        if is_header and current:
            sections.append((current_title, current))
            current_title, current = None, []
        if is_header and current_title is None:
            current_title = str(item["text"]).splitlines()[0].strip()
        current.append(item)
    if current:
        sections.append((current_title, current))
    if not sections and ordered:
        sections = [(None, ordered)]
    return [
        finalize_unit(index, "section", section_items, forced_title=title)
        for index, (title, section_items) in enumerate(sections)
    ]


def finalize_unit(
    index: int,
    unit_type: str,
    items: list[dict[str, Any]],
    *,
    forced_title: str | None,
) -> dict[str, Any]:
    title = (
        forced_title
        or next((item.get("text") for item in items if item.get("text")), None)
        or f"Unit {index + 1}"
    )
    elements = []
    for element_index, item in enumerate(items, start=1):
        elements.append(
            {
                "elementId": f"unit-{index + 1:04d}-element-{element_index:04d}",
                "type": "table"
                if item["kind"] == "table"
                else "image"
                if item["kind"] == "picture"
                else "text",
                "kind": item["kind"],
                "text": {"plain": item.get("text") or "", "paragraphs": [], "runs": []},
                "source": {"doclingLabel": item.get("doclingLabel")},
                "quality": {"reviewRequired": False},
            }
        )
    return {
        "unitId": f"unit-{index + 1:04d}",
        "unitType": unit_type,
        "index": index,
        "title": str(title).splitlines()[0].strip(),
        "sourceRefs": {},
        "render": {"size": {}, "background": {}},
        "speakerNotes": {"raw": "", "cleaned": ""},
        "elements": elements,
    }
