"""Leg 2 — deterministic HTML renderer for a deep-document manifest.

Turns a manifest into a self-contained, viewable HTML page: one card per unit,
content positioned from `bbox`, styled from run-level typography, on the
unit's resolved background. No browser or external renderer is needed to
*produce* the HTML — it is a pure function of the manifest.

DEBUG ARTIFACT — NOT a production viewer (AUDIT F4). `preview.html` proves the
manifest carries enough geometry/typography to reconstruct a document, and is
useful for inspecting extraction output. It is NOT production-fidelity: PPTX
title boxes can overlap, text can clip, and XLSX sheets render sparsely. Those
issues are measured by `layout_diagnostics` and surfaced in
`manifest.diagnostics.layoutDiagnostics`. A production canvas (tldraw) viewer
should consume the manifest through a normalized render contract that resolves
overlap/clipping before shape creation — see PERFORMANCE_CONTRACT.md.

Page-based units (PPTX slides, PDF pages, XLSX sheets) are rendered with
absolute positioning from EMU bboxes. Section-based units (DOCX) have no
geometry, so they fall back to readable flow layout.
"""
from __future__ import annotations

import html
from typing import Any

EMU_PER_INCH = 914400
PX_PER_INCH = 96
DEFAULT_TARGET_WIDTH_PX = 960
DEFAULT_TEXT_COLOR = "#1F2937"
DEFAULT_BACKGROUND = "#FFFFFF"


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _run_style(run: dict[str, Any]) -> str:
    font = run.get("font") or {}
    parts: list[str] = []
    family = font.get("family")
    if family:
        parts.append(f"font-family:{_esc(family)},sans-serif")
    size = font.get("size")
    if size:
        parts.append(f"font-size:{float(size):.1f}pt")
    if font.get("weight") == "bold":
        parts.append("font-weight:700")
    if font.get("italic"):
        parts.append("font-style:italic")
    if font.get("underline") and font.get("underline") != "none":
        parts.append("text-decoration:underline")
    parts.append(f"color:{_esc(run.get('color') or DEFAULT_TEXT_COLOR)}")
    return ";".join(parts)


def _text_html(block: dict[str, Any]) -> str:
    """Render a text block — paragraphs+runs when present, else flat text."""
    paragraphs = block.get("paragraphs")
    if paragraphs:
        out: list[str] = []
        for paragraph in paragraphs:
            align = paragraph.get("alignment") or "left"
            runs = paragraph.get("runs") or []
            spans = "".join(
                f'<span style="{_run_style(r)}">{_esc(r.get("text"))}</span>' for r in runs
            )
            out.append(f'<p style="text-align:{_esc(align)};margin:0">{spans or "&nbsp;"}</p>')
        return "".join(out)
    return f'<p style="margin:0;color:{DEFAULT_TEXT_COLOR}">{_esc(block.get("text"))}</p>'


def _table_html(block: dict[str, Any]) -> str:
    table = block.get("table") or {}
    cells = table.get("cells") or []
    if not cells:
        return '<p style="margin:0;color:#9CA3AF">[table]</p>'
    rows: dict[int, dict[int, dict[str, Any]]] = {}
    for cell in cells:
        rows.setdefault(cell.get("rowIndex", 0), {})[cell.get("colIndex", 0)] = cell
    out = ['<table style="border-collapse:collapse;font-size:11px">']
    for row_index in sorted(rows):
        out.append("<tr>")
        for col_index in sorted(rows[row_index]):
            cell = rows[row_index][col_index]
            tag = "th" if cell.get("isColumnHeader") or cell.get("isRowHeader") else "td"
            out.append(
                f'<{tag} style="border:1px solid #D1D5DB;padding:2px 6px">'
                f'{_esc(cell.get("text"))}</{tag}>'
            )
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _picture_html(block: dict[str, Any], assets_by_id: dict[str, dict[str, Any]]) -> str:
    asset = assets_by_id.get(block.get("assetId") or "")
    local_path = (asset or {}).get("localPath")
    caption = ((asset or {}).get("caption") or {}).get("text")
    if local_path:
        body = f'<img src="file://{_esc(local_path)}" style="max-width:100%;max-height:100%"/>'
    else:
        body = '<div style="color:#9CA3AF;font-size:11px">[image]</div>'
    if caption:
        body += f'<div style="font-size:9px;color:#6B7280">{_esc(caption)}</div>'
    return body


def _emu_box(bbox: dict[str, Any], scale: float) -> str:
    return (
        f"position:absolute;"
        f"left:{bbox.get('x', 0) * scale:.1f}px;"
        f"top:{bbox.get('y', 0) * scale:.1f}px;"
        f"width:{bbox.get('cx', 0) * scale:.1f}px;"
        f"height:{bbox.get('cy', 0) * scale:.1f}px;"
        f"overflow:hidden"
    )


def _block_inner(block: dict[str, Any], assets_by_id: dict[str, dict[str, Any]]) -> str:
    kind = block.get("kind")
    if kind == "table":
        return _table_html(block)
    if kind == "picture":
        return _picture_html(block, assets_by_id)
    return _text_html(block)


def render_unit_html(
    unit: dict[str, Any],
    assets_by_id: dict[str, dict[str, Any]],
    *,
    target_width_px: int = DEFAULT_TARGET_WIDTH_PX,
) -> str:
    """Render one unit (slide/page/section) as an HTML fragment."""
    size = unit.get("pageSizeEmu") or {}
    cx, cy = size.get("cx", 0), size.get("cy", 0)
    background = (unit.get("background") or {}).get("color") or DEFAULT_BACKGROUND
    title = _esc(unit.get("title") or unit.get("unitId"))
    header = f'<div class="unit-title">{title}</div>'

    if cx > 0 and cy > 0:
        # Geometry-bearing unit — absolute layout scaled from EMU.
        scale = target_width_px / cx
        height = cy * scale
        blocks_html = "".join(
            f'<div style="{_emu_box(b.get("bbox") or {}, scale)}">'
            f"{_block_inner(b, assets_by_id)}</div>"
            for b in unit.get("blocks", [])
        )
        canvas = (
            f'<div class="unit-canvas" style="position:relative;'
            f"width:{target_width_px}px;height:{height:.0f}px;"
            f'background:{_esc(background)}">{blocks_html}</div>'
        )
    else:
        # Section unit (DOCX) — no geometry; readable flow layout.
        blocks_html = "".join(
            f'<div class="flow-block">{_block_inner(b, assets_by_id)}</div>'
            for b in unit.get("blocks", [])
        )
        canvas = (
            f'<div class="unit-canvas flow" style="width:{target_width_px}px;'
            f'background:{_esc(background)}">{blocks_html}</div>'
        )
    return f'<section class="unit">{header}{canvas}</section>'


def render_manifest_html(
    manifest: dict[str, Any], *, target_width_px: int = DEFAULT_TARGET_WIDTH_PX
) -> str:
    """Render a whole manifest as one self-contained HTML page."""
    assets_by_id = {a["assetId"]: a for a in manifest.get("assets", []) if a.get("assetId")}
    units_html = "\n".join(
        render_unit_html(unit, assets_by_id, target_width_px=target_width_px)
        for unit in manifest.get("units", [])
    )
    doc_name = _esc(manifest.get("source", {}).get("originalFileName"))
    style = (
        "body{background:#E5E7EB;font-family:sans-serif;margin:0;padding:24px}"
        ".unit{margin:0 auto 32px;max-width:%dpx}"
        ".unit-title{font-size:13px;color:#374151;margin-bottom:6px;font-weight:600}"
        ".unit-canvas{box-shadow:0 1px 4px rgba(0,0,0,.2);border:1px solid #D1D5DB}"
        ".unit-canvas.flow{padding:24px;box-sizing:border-box}"
        ".flow-block{margin-bottom:10px}"
        ".debug-banner{max-width:%dpx;margin:0 auto 16px;padding:8px 12px;"
        "background:#FEF3C7;border:1px solid #F59E0B;border-radius:4px;"
        "font-size:12px;color:#92400E}"
    ) % (target_width_px, target_width_px)
    # AUDIT F4 — preview.html is a debug artifact, not a production viewer.
    banner = (
        '<div class="debug-banner"><strong>Debug preview.</strong> '
        "Structural reconstruction from the manifest — not a production-fidelity "
        "viewer. Text may overlap or clip; see "
        "<code>diagnostics.layoutDiagnostics</code> in the manifest.</div>"
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{doc_name} (debug preview)</title><style>{style}</style></head>"
        f"<body>{banner}"
        f"<h2 style='max-width:{target_width_px}px;margin:0 auto 20px'>{doc_name}</h2>"
        f"{units_html}</body></html>"
    )
