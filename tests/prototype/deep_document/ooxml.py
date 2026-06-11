from __future__ import annotations

import hashlib
import mimetypes
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET


A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
C_NS = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
DGM_NS = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"


# PR1/M2 fix: footer / date / slide-number placeholders are decorations,
# not content. They are captured as unit-level metadata (`decorations`) so
# nothing is lost, but they do not become blocks and do not flow into the
# Bloom taxonomy. Skipping them collapses ~1 phantom block per slide.
DECORATIVE_PLACEHOLDER_TYPES = {"sldNum", "dt", "ftr"}
TITLE_PLACEHOLDER_TYPES = {"title", "ctrTitle", "subTitle"}


@dataclass
class MediaAsset:
    asset_id: str
    kind: str
    mime_type: str
    size_bytes: int
    sha256: str
    extension: str
    source_parts: set[str] = field(default_factory=set)
    used_by: list[dict[str, str]] = field(default_factory=list)
    binary: bytes = b""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_xml(zf: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        return ET.fromstring(zf.read(name))
    except (KeyError, ET.ParseError):
        return None


def extract_text(root: ET.Element | None) -> str:
    if root is None:
        return ""
    parts = [
        (node.text or "").strip()
        for node in root.iter(f"{A_NS}t")
        if node.text and node.text.strip()
    ]
    return "\n".join(parts)


def xml_string(root: ET.Element | None) -> str | None:
    if root is None:
        return None
    return ET.tostring(root, encoding="unicode")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def color_value(node: ET.Element | None, theme: dict[str, Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    from .theme import resolve_scheme_color  # noqa: WPS433

    def _mods(color_node: ET.Element) -> dict[str, int]:
        out = {}
        for child in list(color_node):
            name = local_name(child.tag)
            if name in {"tint", "shade", "alpha", "satMod", "lumMod", "lumOff"} and child.attrib.get("val"):
                out[name] = int(child.attrib["val"])
        return out

    def _apply_mods(hex_color: str | None, mods: dict[str, int]) -> str | None:
        if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
            return hex_color
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        if mods.get("tint") is not None:
            factor = mods["tint"] / 100000
            r = round(r + (255 - r) * factor)
            g = round(g + (255 - g) * factor)
            b = round(b + (255 - b) * factor)
        if mods.get("shade") is not None:
            factor = mods["shade"] / 100000
            r = round(r * factor)
            g = round(g * factor)
            b = round(b * factor)
        return f"#{r:02X}{g:02X}{b:02X}"

    srgb = node.find(f".//{A_NS}srgbClr")
    if srgb is not None and srgb.attrib.get("val"):
        raw_value = f"#{srgb.attrib['val'].upper()}"
        mods = _mods(srgb)
        return {
            "kind": "srgb",
            "value": _apply_mods(raw_value, mods),
            "baseValue": raw_value,
            "modifiers": mods,
            "raw": dict(srgb.attrib),
        }
    sys_clr = node.find(f".//{A_NS}sysClr")
    if sys_clr is not None:
        last = sys_clr.attrib.get("lastClr")
        return {
            "kind": "system",
            "value": f"#{last.upper()}" if last else None,
            "raw": dict(sys_clr.attrib),
        }
    scheme = node.find(f".//{A_NS}schemeClr")
    if scheme is not None and scheme.attrib.get("val"):
        ref = scheme.attrib["val"]
        raw_value = resolve_scheme_color(ref, theme or {})
        mods = _mods(scheme)
        return {
            "kind": "scheme",
            "ref": ref,
            "value": _apply_mods(raw_value, mods),
            "baseValue": raw_value,
            "modifiers": mods,
            "raw": dict(scheme.attrib),
        }
    return None


def fill_style(parent: ET.Element | None, theme: dict[str, Any] | None) -> dict[str, Any] | None:
    if parent is None:
        return None
    if parent.find(f"{A_NS}noFill") is not None:
        return {"kind": "none", "rawXml": xml_string(parent.find(f"{A_NS}noFill"))}
    solid = parent.find(f"{A_NS}solidFill")
    if solid is not None:
        return {"kind": "solid", "color": color_value(solid, theme), "rawXml": xml_string(solid)}
    grad = parent.find(f"{A_NS}gradFill")
    if grad is not None:
        return {"kind": "gradient", "rawXml": xml_string(grad)}
    blip = parent.find(f"{A_NS}blipFill")
    if blip is not None:
        return {"kind": "image", "rawXml": xml_string(blip)}
    return None


def line_style(line: ET.Element | None, theme: dict[str, Any] | None) -> dict[str, Any] | None:
    if line is None:
        return None
    return {
        "widthEmu": int(line.attrib["w"]) if line.attrib.get("w") else None,
        "cap": line.attrib.get("cap"),
        "compound": line.attrib.get("cmpd"),
        "alignment": line.attrib.get("algn"),
        "fill": fill_style(line, theme),
        "dash": (line.find(f"{A_NS}prstDash").attrib.get("val") if line.find(f"{A_NS}prstDash") is not None else None),
        "rawAttrs": dict(line.attrib),
        "rawXml": xml_string(line),
    }


def table_style_part_style(part: ET.Element | None, theme: dict[str, Any] | None) -> dict[str, Any]:
    tx_style = part.find(f"{A_NS}tcTxStyle") if part is not None else None
    tc_style = part.find(f"{A_NS}tcStyle") if part is not None else None
    fill = tc_style.find(f"{A_NS}fill") if tc_style is not None else None
    border_parent = tc_style.find(f"{A_NS}tcBdr") if tc_style is not None else None
    font_ref = tx_style.find(f"{A_NS}fontRef") if tx_style is not None else None
    borders = {}
    if border_parent is not None:
        for tag, side in (("left", "left"), ("right", "right"), ("top", "top"), ("bottom", "bottom")):
            border_node = border_parent.find(f"{A_NS}{tag}")
            borders[side] = line_style(border_node.find(f"{A_NS}ln") if border_node is not None else None, theme)
    return {
        "text": {
            "bold": tx_style.attrib.get("b") if tx_style is not None else None,
            "italic": tx_style.attrib.get("i") if tx_style is not None else None,
            "color": color_value(tx_style, theme),
            "fontRef": {
                "idx": font_ref.attrib.get("idx") if font_ref is not None else None,
                "color": color_value(font_ref, theme),
                "rawAttrs": dict(font_ref.attrib) if font_ref is not None else {},
                "rawXml": xml_string(font_ref),
            },
            "rawAttrs": dict(tx_style.attrib) if tx_style is not None else {},
            "rawXml": xml_string(tx_style),
        },
        "fill": fill_style(fill, theme),
        "borders": borders,
        "rawXml": xml_string(part),
    }


def merge_table_style_parts(*styles: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {"borders": {}, "text": {}, "appliedParts": []}
    for style in styles:
        if not style:
            continue
        if style.get("partName"):
            merged["appliedParts"].append(style["partName"])
        if style.get("fill"):
            merged["fill"] = style["fill"]
        for side, line in (style.get("borders") or {}).items():
            if line:
                merged.setdefault("borders", {})[side] = line
        text_style = style.get("text") or {}
        merged_text = merged.setdefault("text", {})
        for key, value in text_style.items():
            if value not in (None, {}, []):
                merged_text[key] = value
    return merged


def named_table_style_part(
    style_definition: dict[str, Any] | None,
    part_name: str,
) -> dict[str, Any] | None:
    part = ((style_definition or {}).get("parts") or {}).get(part_name)
    if not part:
        return None
    return {"partName": part_name, **part}


def table_cell_effective_style(
    *,
    row_index: int,
    column_index: int,
    column_count: int,
    row_count: int,
    table_properties: dict[str, Any],
    style_definition: dict[str, Any] | None,
    direct_cell_style: dict[str, Any],
) -> dict[str, Any]:
    inherited: list[dict[str, Any] | None] = [
        named_table_style_part(style_definition, "wholeTbl"),
    ]
    if table_properties.get("bandRow"):
        inherited.append(named_table_style_part(style_definition, "band1H" if row_index % 2 == 1 else "band2H"))
    if table_properties.get("bandCol"):
        inherited.append(named_table_style_part(style_definition, "band1V" if column_index % 2 == 1 else "band2V"))
    if table_properties.get("firstRow") and row_index == 0:
        inherited.append(named_table_style_part(style_definition, "firstRow"))
    if table_properties.get("lastRow") and row_index == row_count - 1:
        inherited.append(named_table_style_part(style_definition, "lastRow"))
    if table_properties.get("firstCol") and column_index == 0:
        inherited.append(named_table_style_part(style_definition, "firstCol"))
    if table_properties.get("lastCol") and column_index == column_count - 1:
        inherited.append(named_table_style_part(style_definition, "lastCol"))
    return merge_table_style_parts(*inherited, direct_cell_style)


def parse_table_styles(zf: zipfile.ZipFile, theme: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    root = read_xml(zf, "ppt/tableStyles.xml")
    if root is None:
        return {}
    styles: dict[str, dict[str, Any]] = {}
    for style in root.findall(f"{A_NS}tblStyle"):
        style_id = style.attrib.get("styleId")
        if not style_id:
            continue
        parts = {
            local_name(part.tag): table_style_part_style(part, theme)
            for part in list(style)
        }
        styles[style_id] = {
            "styleId": style_id,
            "styleName": style.attrib.get("styleName"),
            "rawAttrs": dict(style.attrib),
            "rawXml": xml_string(style),
            "parts": parts,
        }
    return styles


def body_pr_style(body_pr: ET.Element | None) -> dict[str, Any] | None:
    if body_pr is None:
        return None
    return {
        "rawAttrs": dict(body_pr.attrib),
        "insetsEmu": {
            "left": int(body_pr.attrib.get("lIns", "0")),
            "top": int(body_pr.attrib.get("tIns", "0")),
            "right": int(body_pr.attrib.get("rIns", "0")),
            "bottom": int(body_pr.attrib.get("bIns", "0")),
        },
        "anchor": body_pr.attrib.get("anchor"),
        "wrap": body_pr.attrib.get("wrap"),
        "vertical": body_pr.attrib.get("vert"),
        "rawXml": xml_string(body_pr),
    }


def shape_style(element: ET.Element, theme: dict[str, Any] | None) -> dict[str, Any]:
    sp_pr = element.find(f"{P_NS}spPr")
    if sp_pr is None:
        sp_pr = element.find(f"{P_NS}picPr")
    if sp_pr is None:
        sp_pr = element.find(f"{P_NS}grpSpPr")
    return {
        "shapeProperties": {
            "fill": fill_style(sp_pr, theme),
            "line": line_style(sp_pr.find(f"{A_NS}ln"), theme) if sp_pr is not None else None,
            "rawAttrs": dict(sp_pr.attrib) if sp_pr is not None else {},
            "rawXml": xml_string(sp_pr),
        },
        "textBody": {
            "bodyPr": body_pr_style(_tx_body(element).find(f"{A_NS}bodyPr") if _tx_body(element) is not None else None),
            "listStyleRawXml": xml_string(_tx_body(element).find(f"{A_NS}lstStyle") if _tx_body(element) is not None else None),
        },
    }


def paragraph_text(paragraph: dict[str, Any]) -> str:
    return "".join(run.get("text", "") for run in paragraph.get("runs", []))


def should_apply_default_placeholder_bullet(
    *,
    ph_type: str | None,
    ph_idx: str | None,
    name: str,
) -> bool:
    if ph_type in TITLE_PLACEHOLDER_TYPES:
        return False
    if ph_type in {"body", "obj"}:
        return True
    return bool(ph_idx is not None and "placeholder" in name.lower())


def apply_default_placeholder_bullets(
    paragraphs: list[dict[str, Any]],
    *,
    ph_type: str | None,
    ph_idx: str | None,
    name: str,
) -> None:
    if not should_apply_default_placeholder_bullet(ph_type=ph_type, ph_idx=ph_idx, name=name):
        return
    for paragraph in paragraphs:
        if not paragraph_text(paragraph).strip():
            continue
        if paragraph.get("alignment") == "center":
            continue
        if paragraph.get("bullet") is None:
            paragraph["bullet"] = {"kind": "char", "char": "•", "numberFormat": None}


def rels_for(zf: zipfile.ZipFile, part_name: str) -> dict[str, dict[str, str]]:
    part = PurePosixPath(part_name)
    rels_name = str(part.parent / "_rels" / f"{part.name}.rels")
    root = read_xml(zf, rels_name)
    if root is None:
        return {}
    rels: dict[str, dict[str, str]] = {}
    for rel in root.findall(f"{REL_NS}Relationship"):
        rel_id = rel.attrib.get("Id")
        if rel_id:
            rels[rel_id] = {
                "type": rel.attrib.get("Type", ""),
                "target": rel.attrib.get("Target", ""),
            }
    return rels


def normalize_part_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        target = target[1:]
    base = PurePosixPath(source_part).parent
    pieces: list[str] = []
    for piece in str(base / target).split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if pieces:
                pieces.pop()
        else:
            pieces.append(piece)
    return "/".join(pieces)


def presentation_slide_parts(zf: zipfile.ZipFile) -> list[str]:
    presentation = read_xml(zf, "ppt/presentation.xml")
    presentation_rels = rels_for(zf, "ppt/presentation.xml")
    ordered: list[str] = []
    if presentation is not None:
        for slide_id in presentation.findall(f".//{P_NS}sldId"):
            rel_id = slide_id.attrib.get(f"{R_NS}id")
            rel = presentation_rels.get(rel_id or "")
            if rel and rel["type"].endswith("/slide"):
                part = normalize_part_target("ppt/presentation.xml", rel["target"])
                if part in zf.namelist():
                    ordered.append(part)
    if ordered:
        return ordered

    slide_parts = []
    for name in zf.namelist():
        match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
        if match:
            slide_parts.append((int(match.group(1)), name))
    return [name for _, name in sorted(slide_parts)]


def slide_number(part_name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", part_name)
    return int(match.group(1)) if match else 0


def slide_size(zf: zipfile.ZipFile) -> dict[str, int]:
    presentation = read_xml(zf, "ppt/presentation.xml")
    if presentation is None:
        return {"cx": 0, "cy": 0}
    size = presentation.find(f".//{P_NS}sldSz")
    if size is None:
        return {"cx": 0, "cy": 0}
    return {"cx": int(size.attrib.get("cx", "0")), "cy": int(size.attrib.get("cy", "0"))}


def layout_name(zf: zipfile.ZipFile, slide_part: str, slide_rels: dict[str, dict[str, str]]) -> str | None:
    for rel in slide_rels.values():
        if rel["type"].endswith("/slideLayout"):
            target = normalize_part_target(slide_part, rel["target"])
            root = read_xml(zf, target)
            if root is None:
                return Path(target).stem
            c_sld = root.find(f".//{P_NS}cSld")
            return c_sld.attrib.get("name") if c_sld is not None else Path(target).stem
    return None


def clean_notes(raw: str, slide_no: int) -> tuple[str | None, bool]:
    """Strip junk lines from speaker notes.

    Filters: empty lines, lines that are just the slide number (auto-inserted
    by some PPTX templates), and whitespace runs. Returns (cleaned, changed)
    where `changed=True` means the cleaner removed something.
    """
    lines = []
    for line in raw.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if not cleaned:
            continue
        if cleaned == str(slide_no):
            continue
        lines.append(cleaned)
    result = "\n".join(lines).strip()
    return (result or None, raw.strip() != result)


def bbox_for(element: ET.Element) -> dict[str, int]:
    xfrm = element.find(f".//{A_NS}xfrm")
    if xfrm is None:
        xfrm = element.find(f".//{P_NS}xfrm")
    if xfrm is None:
        return {"x": 0, "y": 0, "cx": 0, "cy": 0}
    off = xfrm.find(f"{A_NS}off")
    if off is None:
        off = xfrm.find(f"{P_NS}off")
    ext = xfrm.find(f"{A_NS}ext")
    if ext is None:
        ext = xfrm.find(f"{P_NS}ext")
    return {
        "x": int(off.attrib.get("x", "0")) if off is not None else 0,
        "y": int(off.attrib.get("y", "0")) if off is not None else 0,
        "cx": int(ext.attrib.get("cx", "0")) if ext is not None else 0,
        "cy": int(ext.attrib.get("cy", "0")) if ext is not None else 0,
    }


def table_rows(element: ET.Element) -> list[list[str]]:
    table = element.find(f".//{A_NS}tbl")
    if table is None:
        return []
    rows: list[list[str]] = []
    for row in table.findall(f"{A_NS}tr"):
        cells = []
        for cell in row.findall(f"{A_NS}tc"):
            cells.append(extract_text(cell))
        rows.append(cells)
    return rows


def table_cell_style(cell: ET.Element, theme: dict[str, Any] | None) -> dict[str, Any]:
    tc_pr = cell.find(f"{A_NS}tcPr")
    borders = {}
    if tc_pr is not None:
        for tag, side in (("lnL", "left"), ("lnR", "right"), ("lnT", "top"), ("lnB", "bottom")):
            borders[side] = line_style(tc_pr.find(f"{A_NS}{tag}"), theme)
    return {
        "fill": fill_style(tc_pr, theme),
        "borders": borders,
        "marginsEmu": {
            "left": int(tc_pr.attrib["marL"]) if tc_pr is not None and tc_pr.attrib.get("marL") else None,
            "right": int(tc_pr.attrib["marR"]) if tc_pr is not None and tc_pr.attrib.get("marR") else None,
            "top": int(tc_pr.attrib["marT"]) if tc_pr is not None and tc_pr.attrib.get("marT") else None,
            "bottom": int(tc_pr.attrib["marB"]) if tc_pr is not None and tc_pr.attrib.get("marB") else None,
        },
        "verticalAnchor": tc_pr.attrib.get("anchor") if tc_pr is not None else None,
        "rawAttrs": dict(tc_pr.attrib) if tc_pr is not None else {},
        "rawXml": xml_string(tc_pr),
    }


def table_model(
    element: ET.Element,
    theme: dict[str, Any] | None,
    table_styles: dict[str, dict[str, Any]],
    parse_paragraphs_fn: Callable[[ET.Element | None, dict[str, Any], Any], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    table = element.find(f".//{A_NS}tbl")
    if table is None:
        return None
    tbl_pr = table.find(f"{A_NS}tblPr")
    style_id = None
    if tbl_pr is not None:
        style = tbl_pr.find(f"{A_NS}tableStyleId")
        style_id = style.text if style is not None else None
    style_definition = table_styles.get(style_id or "")
    table_properties = {
        "firstRow": tbl_pr.attrib.get("firstRow") == "1" if tbl_pr is not None else False,
        "bandRow": tbl_pr.attrib.get("bandRow") == "1" if tbl_pr is not None else False,
        "firstCol": tbl_pr.attrib.get("firstCol") == "1" if tbl_pr is not None else False,
        "lastRow": tbl_pr.attrib.get("lastRow") == "1" if tbl_pr is not None else False,
        "lastCol": tbl_pr.attrib.get("lastCol") == "1" if tbl_pr is not None else False,
        "bandCol": tbl_pr.attrib.get("bandCol") == "1" if tbl_pr is not None else False,
        "rawAttrs": dict(tbl_pr.attrib) if tbl_pr is not None else {},
        "rawXml": xml_string(tbl_pr),
    }
    grid = table.find(f"{A_NS}tblGrid")
    columns = [
        {
            "index": index,
            "widthEmu": int(col.attrib.get("w", "0")),
            "rawAttrs": dict(col.attrib),
            "rawXml": xml_string(col),
        }
        for index, col in enumerate(grid.findall(f"{A_NS}gridCol") if grid is not None else [])
    ]
    rows = []
    for row_index, row in enumerate(table.findall(f"{A_NS}tr")):
        cells = []
        row_cells = row.findall(f"{A_NS}tc")
        for col_index, cell in enumerate(row.findall(f"{A_NS}tc")):
            tx_body = cell.find(f"{A_NS}txBody")
            paragraphs = parse_paragraphs_fn(tx_body, theme or {}, None)
            direct_style = table_cell_style(cell, theme)
            cells.append(
                {
                    "rowIndex": row_index,
                    "columnIndex": col_index,
                    "text": extract_text(cell),
                    "paragraphs": paragraphs,
                    "style": direct_style,
                    "effectiveStyle": table_cell_effective_style(
                        row_index=row_index,
                        column_index=col_index,
                        column_count=len(row_cells),
                        row_count=len(table.findall(f"{A_NS}tr")),
                        table_properties=table_properties,
                        style_definition=style_definition,
                        direct_cell_style=direct_style,
                    ),
                    "rawXml": xml_string(cell),
                }
            )
        rows.append(
            {
                "rowIndex": row_index,
                "heightEmu": int(row.attrib.get("h", "0")),
                "rawAttrs": dict(row.attrib),
                "rawXml": xml_string(row),
                "cells": cells,
            }
        )
    return {
        "styleId": style_id,
        "styleDefinition": style_definition,
        "properties": table_properties,
        "columns": columns,
        "rows": rows,
        "rawXml": xml_string(table),
    }


def placeholder_type(element: ET.Element) -> str | None:
    ph = element.find(f".//{P_NS}ph")
    return ph.attrib.get("type") if ph is not None else None


def shape_name(element: ET.Element) -> str | None:
    c_nv_pr = element.find(f".//{P_NS}cNvPr")
    return c_nv_pr.attrib.get("name") if c_nv_pr is not None else None


def rel_id_for_picture(element: ET.Element) -> str | None:
    blip = element.find(f".//{A_NS}blip")
    if blip is None:
        return None
    return blip.attrib.get(f"{R_NS}embed") or blip.attrib.get(f"{R_NS}link")


def graphic_frame_kind(element: ET.Element) -> str:
    if element.find(f".//{A_NS}tbl") is not None:
        return "table"
    if element.find(f".//{C_NS}chart") is not None:
        return "chart_placeholder"
    if element.find(f".//{DGM_NS}relIds") is not None:
        return "smartart_placeholder"
    return "other"


def direct_renderable_shapes(slide_root: ET.Element | None) -> list[ET.Element]:
    """Collect renderable shapes, recursing into group shapes.

    Group recursion matters for the typography join: Docling descends into
    `<p:grpSp>` and surfaces group-nested text as its own items, so OOXML must
    descend too or that text gets no typography. The group container is kept
    (harmless — no text body); its nested shapes are also emitted. Nested-shape
    bboxes are in group child-coordinate space and are NOT used — the Docling
    spine owns geometry; this walk only feeds typography harvest.
    """
    if slide_root is None:
        return []
    sp_tree = slide_root.find(f".//{P_NS}spTree")
    if sp_tree is None:
        return []
    renderable = {f"{P_NS}sp", f"{P_NS}pic", f"{P_NS}graphicFrame", f"{P_NS}grpSp"}
    shapes: list[ET.Element] = []

    def _collect(container: ET.Element) -> None:
        for child in list(container):
            if child.tag in renderable:
                shapes.append(child)
                if child.tag == f"{P_NS}grpSp":
                    _collect(child)

    _collect(sp_tree)
    return shapes


def _mime_for(source_part: str) -> str:
    guessed, _ = mimetypes.guess_type(source_part)
    if guessed:
        return guessed
    suffix = Path(source_part).suffix.lower()
    if suffix == ".emf":
        return "image/x-emf"
    if suffix == ".wmf":
        return "image/wmf"
    return "application/octet-stream"


def _tx_body(shape: ET.Element) -> ET.Element | None:
    """Find the text body inside a shape, regardless of which namespace owns it."""
    for tag in (f"{P_NS}txBody", f"{A_NS}txBody"):
        body = shape.find(tag)
        if body is not None:
            return body
    return None


def _placeholder_ref(shape: ET.Element) -> tuple[str | None, str | None]:
    """Return the (type, idx) of a shape's placeholder, or (None, None)."""
    ph = shape.find(f".//{P_NS}ph")
    if ph is None:
        return (None, None)
    return (ph.attrib.get("type"), ph.attrib.get("idx"))


def parse_pptx(
    path: Path,
    theme: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, MediaAsset], dict[str, int]]:
    """Parse the PPTX slides + media.

    When `theme` is provided:
    - Every text block carries `paragraphs` with run-level typography,
      resolved through the full layout/master inheritance chain (experiment5).
    - Each unit carries `background` resolved from slide → layout → master.
    """
    # Lazy imports to avoid a circular dependency at module load time.
    from .background import slide_background  # noqa: WPS433
    from .placeholder_resolver import build_slide_resolver  # noqa: WPS433
    from .text_styles import parse_presentation_default_style  # noqa: WPS433
    from .typography import joined_text, parse_paragraphs  # noqa: WPS433

    assets: dict[str, MediaAsset] = {}
    slides: list[dict[str, Any]] = []
    # Shared across slides: masters are parsed once, not per-slide.
    master_cache: dict[str, Any] = {}
    with zipfile.ZipFile(path) as zf:
        size = slide_size(zf)
        parts = presentation_slide_parts(zf)
        presentation_default = (
            parse_presentation_default_style(zf, theme) if theme is not None else {}
        )
        table_styles = parse_table_styles(zf, theme)
        for index, slide_part in enumerate(parts):
            number = slide_number(slide_part)
            unit_id = f"slide-{index + 1:03d}"
            root = read_xml(zf, slide_part)
            slide_rels = rels_for(zf, slide_part)
            notes_raw = ""
            for rel in slide_rels.values():
                if rel["type"].endswith("/notesSlide"):
                    notes_raw = extract_text(read_xml(zf, normalize_part_target(slide_part, rel["target"])))

            blocks: list[dict[str, Any]] = []
            title: str | None = None
            decorations: dict[str, str] = {}

            style_resolver = (
                build_slide_resolver(
                    zf, slide_part, slide_rels, theme, master_cache, presentation_default
                )
                if theme is not None
                else None
            )

            for shape_index, shape in enumerate(direct_renderable_shapes(root), start=1):
                ph_type, ph_idx = _placeholder_ref(shape)

                # PR1/M2: route decorative placeholders to unit metadata, not blocks.
                if ph_type in DECORATIVE_PLACEHOLDER_TYPES:
                    deco_text = extract_text(shape)
                    if deco_text:
                        key = {"sldNum": "slideNumberText", "dt": "dateText", "ftr": "footerText"}[ph_type]
                        decorations[key] = deco_text
                    continue

                kind = "other"
                paragraphs: list[dict[str, Any]] = []
                if theme is not None:
                    style_context = (
                        style_resolver.context_for(ph_type, ph_idx)
                        if style_resolver is not None
                        else None
                    )
                    paragraphs = parse_paragraphs(_tx_body(shape), theme, style_context)
                    apply_default_placeholder_bullets(
                        paragraphs,
                        ph_type=ph_type,
                        ph_idx=ph_idx,
                        name=shape_name(shape),
                    )
                text = joined_text(paragraphs) if paragraphs else extract_text(shape)
                asset_id = None
                relationship_id = None
                table_ref = None
                rows: list[list[str]] = []
                table: dict[str, Any] | None = None

                if shape.tag == f"{P_NS}sp":
                    kind = "text" if text else "other"
                elif shape.tag == f"{P_NS}pic":
                    kind = "picture"
                    relationship_id = rel_id_for_picture(shape)
                    rel = slide_rels.get(relationship_id or "")
                    if rel:
                        target = normalize_part_target(slide_part, rel["target"])
                        if target in zf.namelist():
                            binary = zf.read(target)
                            sha = sha256_bytes(binary)
                            suffix = Path(target).suffix.lower() or ".bin"
                            asset_id = f"asset-{sha[:8]}"
                            asset = assets.get(asset_id)
                            if asset is None:
                                asset = MediaAsset(
                                    asset_id=asset_id,
                                    kind="image" if _mime_for(target).startswith("image/") else "binary",
                                    mime_type=_mime_for(target),
                                    size_bytes=len(binary),
                                    sha256=sha,
                                    extension=suffix,
                                    binary=binary,
                                )
                                assets[asset_id] = asset
                            asset.source_parts.add(target)
                elif shape.tag == f"{P_NS}graphicFrame":
                    kind = graphic_frame_kind(shape)
                    table_ref = f"{unit_id}-shape-{shape_index:03d}" if kind == "table" else None
                    table = table_model(shape, theme, table_styles, parse_paragraphs) if kind == "table" else None
                    rows = [
                        [cell.get("text", "") for cell in row.get("cells", [])]
                        for row in (table or {}).get("rows", [])
                    ]
                elif shape.tag == f"{P_NS}grpSp":
                    kind = "group"

                block_id = f"{unit_id}-block-{len(blocks) + 1:03d}"
                block = {
                    "blockId": block_id,
                    "kind": kind,
                    "ooxmlShapeIndex": shape_index,
                    "placeholderType": ph_type,
                    "bbox": bbox_for(shape),
                    "zOrder": len(blocks),
                    "text": text or None,
                    "tableRef": table_ref,
                    "assetId": asset_id,
                    "relationshipId": relationship_id,
                    "shapeName": shape_name(shape),
                    "style": shape_style(shape, theme),
                }
                if rows:
                    block["rows"] = rows
                if table:
                    block["table"] = table
                if paragraphs:
                    block["paragraphs"] = paragraphs
                blocks.append(block)
                if asset_id:
                    assets[asset_id].used_by.append(
                        {"unitId": unit_id, "blockId": block_id, "relationshipId": relationship_id or ""}
                    )
                if ph_type in {"title", "ctrTitle"} and text and title is None:
                    title = next((line.strip() for line in text.splitlines() if line.strip()), None)

            if title is None:
                first_text = next((block["text"] for block in blocks if block.get("text")), None)
                title = (
                    next((line.strip() for line in first_text.splitlines() if line.strip()), None)
                    if first_text
                    else None
                )

            cleaned_notes, junk_filtered = clean_notes(notes_raw, number or index + 1)
            background = (
                slide_background(zf, slide_part, slide_rels, theme, assets)
                if theme is not None
                else {"kind": "none", "color": None, "assetId": None, "source": "master"}
            )
            slides.append(
                {
                    "unitId": unit_id,
                    "unitType": "slide",
                    "index": index,
                    "slideNumber": number or index + 1,
                    "sourcePart": slide_part,
                    "title": title,
                    "layoutName": layout_name(zf, slide_part, slide_rels),
                    "slideSizeEmu": size,
                    "decorations": decorations,
                    "background": background,
                    "blocks": blocks,
                    "speakerNotes": {
                        "raw": notes_raw,
                        "cleaned": cleaned_notes,
                        "junkFiltered": junk_filtered,
                    },
                }
            )

    stats = {
        "slideCount": len(slides),
        "ooxmlMediaCount": len(assets),
        "ooxmlNotesSlideCount": sum(1 for slide in slides if slide["speakerNotes"]["raw"].strip()),
        "ooxmlDecorationSlideCount": sum(1 for slide in slides if slide["decorations"]),
    }
    return slides, assets, stats
