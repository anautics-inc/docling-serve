"""Docling-centric structural spine.

Reads a `DoclingDocument` JSON (schema 1.x) and produces the format-agnostic
`units` + `blocks` structure that the rest of the pipeline consumes. This is
the spine: it works the same for PPTX slides, PDF pages, DOCX sections, and
XLSX sheets, because every one of those becomes Docling `pages` + items with
`prov[].page_no`.

Content is assigned to a unit by `prov[].page_no` — NOT by parsing Docling's
PPTX-only `slide-N` group names. Page provenance is universal; group names
are a backend artifact and relying on them would couple the spine to PPTX.
"""
from __future__ import annotations

from typing import Any


# DoclingDocument item collections that carry renderable content.
CONTENT_COLLECTIONS = ("texts", "tables", "pictures")

# Docling text `label` values that are genuine titles.
TITLE_LABELS = frozenset({"title", "section_header", "page_header"})


def normalize_bbox(bbox: dict[str, Any], page_height: float) -> dict[str, int]:
    """Convert a Docling `prov[].bbox` to the manifest's TOPLEFT `{x,y,cx,cy}`.

    Docling bboxes are `{l, t, r, b, coord_origin}`. For BOTTOMLEFT (the PPTX
    default) `t`/`b` are measured up from the page bottom, so `t > b`. The
    single source of truth for the flip — never inline it elsewhere.
    """
    left = float(bbox.get("l", 0.0))
    top = float(bbox.get("t", 0.0))
    right = float(bbox.get("r", 0.0))
    bottom = float(bbox.get("b", 0.0))
    origin = bbox.get("coord_origin", "BOTTOMLEFT")
    if origin == "BOTTOMLEFT":
        x = left
        y = page_height - top
        cx = right - left
        cy = top - bottom
    else:  # TOPLEFT
        x = left
        y = top
        cx = right - left
        cy = bottom - top
    return {
        "x": round(x),
        "y": round(y),
        "cx": round(abs(cx)),
        "cy": round(abs(cy)),
    }


def _page_of(item: dict[str, Any]) -> int | None:
    prov = item.get("prov")
    if isinstance(prov, list) and prov:
        page_no = prov[0].get("page_no")
        if page_no is not None:
            return int(page_no)
    return None


def _bbox_of(item: dict[str, Any]) -> dict[str, Any] | None:
    prov = item.get("prov")
    if isinstance(prov, list) and prov:
        return prov[0].get("bbox")
    return None


def _reading_order(doc: dict[str, Any]) -> dict[str, int]:
    """Walk the `body` tree to produce a global reading-order index per item ref.

    Items not reached by the walk (rare — orphaned refs) get an index after
    everything else, preserving their array order as a stable fallback.
    """
    order: dict[str, int] = {}
    counter = 0

    ref_index: dict[str, dict[str, Any]] = {}
    for collection in (*CONTENT_COLLECTIONS, "groups"):
        for idx, item in enumerate(doc.get(collection, [])):
            if isinstance(item, dict) and item.get("self_ref"):
                ref_index[item["self_ref"]] = item

    def visit(ref: str) -> None:
        nonlocal counter
        if ref in order:
            return
        node = ref_index.get(ref)
        if node is None:
            return
        if ref.startswith("#/groups/"):
            for child in node.get("children", []) or []:
                child_ref = child.get("$ref") if isinstance(child, dict) else None
                if child_ref:
                    visit(child_ref)
        else:
            order[ref] = counter
            counter += 1

    body = doc.get("body", {})
    for child in body.get("children", []) or []:
        child_ref = child.get("$ref") if isinstance(child, dict) else None
        if child_ref:
            visit(child_ref)
    return order


def _table_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Extract real cell structure from a Docling table — the gap experiments
    2–5 deferred. Docling already parsed every cell.
    """
    data = item.get("data") or {}
    cells = []
    for cell in data.get("table_cells", []) or []:
        cells.append(
            {
                "text": cell.get("text", ""),
                "rowIndex": cell.get("start_row_offset_idx"),
                "colIndex": cell.get("start_col_offset_idx"),
                "rowSpan": cell.get("row_span", 1),
                "colSpan": cell.get("col_span", 1),
                "isColumnHeader": bool(cell.get("column_header")),
                "isRowHeader": bool(cell.get("row_header")),
            }
        )
    return {
        "numRows": data.get("num_rows"),
        "numCols": data.get("num_cols"),
        "cells": cells,
    }


def parse_docling_document(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the units + blocks spine from a DoclingDocument JSON dict.

    Returns one unit per page, each with `blocks` ordered by document reading
    order. Format-agnostic: a PDF or XLSX DoclingDocument produces the same
    shape.
    """
    pages = doc.get("pages", {}) or {}
    # Page sizes, keyed by int page number.
    page_size: dict[int, dict[str, float]] = {}
    for key, page in pages.items():
        try:
            page_no = int(page.get("page_no", key))
        except (TypeError, ValueError):
            continue
        size = page.get("size") or {}
        page_size[page_no] = {
            "width": float(size.get("width", 0.0)),
            "height": float(size.get("height", 0.0)),
        }

    order = _reading_order(doc)

    # Collect every content item into one flat list, each tagged with its
    # page (may be None — DOCX has no pages) and reading order. Page bucketing
    # OR section segmentation happens afterward depending on the format.
    collected: list[dict[str, Any]] = []

    def _emit(item: dict[str, Any], kind: str, extra: dict[str, Any]) -> None:
        page_no = _page_of(item)
        raw_bbox = _bbox_of(item)
        height = (page_size.get(page_no, {}).get("height") if page_no else None) or 1.0
        bbox = normalize_bbox(raw_bbox, height) if raw_bbox else {"x": 0, "y": 0, "cx": 0, "cy": 0}
        collected.append(
            {
                "doclingRef": item.get("self_ref"),
                "kind": kind,
                "doclingLabel": item.get("label"),
                "bbox": bbox,
                "pageNo": page_no,
                "readingOrder": order.get(item.get("self_ref", ""), 10**9),
                **extra,
            }
        )

    for item in doc.get("texts", []) or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or item.get("orig") or "").strip()
        _emit(item, "text", {"text": text or None})

    for item in doc.get("tables", []) or []:
        if not isinstance(item, dict):
            continue
        _emit(item, "table", {"text": None, "table": _table_payload(item)})

    for pic_index, item in enumerate(doc.get("pictures", []) or []):
        if not isinstance(item, dict):
            continue
        image = item.get("image") or {}
        _emit(
            item,
            "picture",
            {
                "text": None,
                "image": {
                    "mimeType": image.get("mimetype"),
                    # `uri` is an absolute path baked at extraction time and is
                    # often stale (Docling's temp dir gets deleted). `pictureIndex`
                    # is the stable handle — callers resolve the real file by
                    # index from the Docling output directory.
                    "uri": image.get("uri"),
                    "pictureIndex": pic_index,
                    "dpi": image.get("dpi"),
                    "width": (image.get("size") or {}).get("width"),
                    "height": (image.get("size") or {}).get("height"),
                },
            },
        )

    if page_size:
        return _page_units(collected, page_size)
    return _section_units(collected)


def _finalize_unit(
    index: int,
    *,
    unit_type: str,
    page_number: int,
    page_size_emu: dict[str, int],
    raw_blocks: list[dict[str, Any]],
    forced_title: str | None = None,
) -> dict[str, Any]:
    """Assemble one unit with a shape consistent across every format.

    Every unit carries `speakerNotes` and `background` neutral defaults so the
    semantic layer never KeyErrors on a non-PPTX document. PPTX OOXML
    enrichment overwrites these; other formats keep the neutral values.
    """
    blocks: list[dict[str, Any]] = []
    title: str | None = forced_title
    for block_index, raw in enumerate(raw_blocks, start=1):
        block = {"blockId": f"unit-{index + 1:04d}-block-{block_index:04d}", **raw}
        block.pop("pageNo", None)  # internal bucketing key — not part of the block
        blocks.append(block)
        if (
            title is None
            and raw["kind"] == "text"
            and raw.get("doclingLabel") in TITLE_LABELS
            and raw.get("text")
        ):
            title = raw["text"].splitlines()[0].strip()
    if title is None:
        first_text = next((b.get("text") for b in blocks if b.get("text")), None)
        title = first_text.splitlines()[0].strip() if first_text else None
    return {
        "unitId": f"unit-{index + 1:04d}",
        "unitType": unit_type,
        "index": index,
        "pageNumber": page_number,
        "title": title,
        "pageSizeEmu": page_size_emu,
        "blocks": blocks,
        "speakerNotes": {"raw": "", "cleaned": None, "junkFiltered": False},
        "background": {"kind": "none", "color": None, "assetId": None, "source": "master"},
    }


def _page_units(
    collected: list[dict[str, Any]], page_size: dict[int, dict[str, float]]
) -> list[dict[str, Any]]:
    """Page-based units — PPTX slides, PDF pages, XLSX sheets."""
    by_page: dict[int, list[dict[str, Any]]] = {p: [] for p in page_size}
    for item in collected:
        page_no = item.get("pageNo")
        if page_no in by_page:
            by_page[page_no].append(item)
    units: list[dict[str, Any]] = []
    for index, page_no in enumerate(sorted(page_size)):
        size = page_size[page_no]
        raw_blocks = sorted(by_page.get(page_no, []), key=lambda b: b["readingOrder"])
        units.append(
            _finalize_unit(
                index,
                unit_type="page",
                page_number=page_no,
                page_size_emu={"cx": round(size["width"]), "cy": round(size["height"])},
                raw_blocks=raw_blocks,
            )
        )
    return units


def _section_units(collected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Section-based units — DOCX and any DoclingDocument with no `pages`.

    Word has no inherent pagination; segment the reading-order stream at
    `title`/`section_header` boundaries so each section becomes one unit.
    Content before the first header forms a leading section.
    """
    ordered = sorted(collected, key=lambda b: b["readingOrder"])
    sections: list[tuple[str | None, list[dict[str, Any]]]] = []
    current_title: str | None = None
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
            current_title = item["text"].splitlines()[0].strip()
        current.append(item)
    if current:
        sections.append((current_title, current))
    if not sections and ordered:
        sections = [(None, ordered)]  # no headers at all — one document unit

    return [
        _finalize_unit(
            index,
            unit_type="section",
            page_number=index + 1,
            page_size_emu={"cx": 0, "cy": 0},
            raw_blocks=items,
            forced_title=title,
        )
        for index, (title, items) in enumerate(sections)
    ]


def spine_summary(units: list[dict[str, Any]]) -> dict[str, int]:
    """Quick counts for coverage/diagnostics — no per-format assumptions."""
    kinds: dict[str, int] = {}
    for unit in units:
        for block in unit["blocks"]:
            kinds[block["kind"]] = kinds.get(block["kind"], 0) + 1
    return {
        "units": len(units),
        "blocks": sum(len(u["blocks"]) for u in units),
        "textBlocks": kinds.get("text", 0),
        "tableBlocks": kinds.get("table", 0),
        "pictureBlocks": kinds.get("picture", 0),
        "titledUnits": sum(1 for u in units if u["title"]),
    }
