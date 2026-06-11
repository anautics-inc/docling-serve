"""Parse `<p:txBody>` into structured paragraphs, resolving the full PowerPoint
text-style inheritance chain.

Experiment5 rewrite. Where experiment4 only read slide-local run/paragraph/
list-style properties, this version accepts a `StyleContext` carrying the
layout placeholder, master placeholder, master text style, and presentation
default levels. Each run's font/color is resolved by walking that chain;
`resolvedFrom` records which level supplied each value.
"""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET


A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

ALIGNMENT_MAP = {
    "l": "left",
    "ctr": "center",
    "r": "right",
    "just": "justify",
    "dist": "justify",
    "thaiDist": "justify",
}

UNDERLINE_MAP = {
    "none": "none",
    "sng": "single",
    "dbl": "double",
    "heavy": "single",
    "dotted": "single",
    "dottedHeavy": "single",
    "dash": "single",
    "dashHeavy": "single",
    "wavy": "single",
    "wavyHeavy": "single",
    "wavyDbl": "double",
}

# `<a:latin typeface="+mj-lt"/>` is a theme reference, not a literal font.
THEME_FONT_REFS = {
    "+mj-lt": ("majorFont", "latin"),
    "+mn-lt": ("minorFont", "latin"),
    "+mj-ea": ("majorFont", "ea"),
    "+mn-ea": ("minorFont", "ea"),
    "+mj-cs": ("majorFont", "cs"),
    "+mn-cs": ("minorFont", "cs"),
}

FONT_FIELDS = ("family", "size", "weight", "italic", "underline")
# Documented PowerPoint defaults applied when no chain level sets the field.
FIELD_DEFAULTS = {"weight": "normal", "italic": False, "underline": "none"}


def _bool_attr(value: str | None) -> bool:
    return value in {"1", "true", "True"}


def resolve_theme_font(typeface: str, theme: dict[str, Any]) -> tuple[str | None, bool]:
    """Resolve a `<a:latin typeface=...>` value. Returns (font_name, is_theme_ref)."""
    ref = THEME_FONT_REFS.get(typeface)
    if ref is None:
        return typeface, False
    font_scheme = theme.get("fontScheme") or {}
    block = font_scheme.get(ref[0]) or {}
    return (block.get(ref[1]) or None), True


def _color_from_props(elem: ET.Element, theme: dict[str, Any]) -> str | None:
    """Resolve a run/defRPr color: srgbClr | sysClr | schemeClr (theme-resolved)."""
    from .theme import resolve_scheme_color  # local import — avoids load-time cycle

    solid = elem.find(f"{A_NS}solidFill")
    target = solid if solid is not None else elem
    srgb = target.find(f"{A_NS}srgbClr")
    if srgb is not None and srgb.attrib.get("val"):
        return f"#{srgb.attrib['val'].upper()}"
    sys_clr = target.find(f"{A_NS}sysClr")
    if sys_clr is not None and sys_clr.attrib.get("lastClr"):
        return f"#{sys_clr.attrib['lastClr'].upper()}"
    scheme = target.find(f"{A_NS}schemeClr")
    if scheme is not None and scheme.attrib.get("val"):
        return resolve_scheme_color(scheme.attrib["val"], theme)
    return None


def extract_run_props(elem: ET.Element | None, theme: dict[str, Any]) -> dict[str, Any]:
    """Extract typography props from `<a:rPr>` / `<a:defRPr>` / `<a:endParaRPr>`.

    Only keys that the element actually sets are present in the returned dict.
    `familySource` is an internal marker ("themeFont" when the font name came
    from a `+mj-lt`-style reference); callers strip it before emitting.
    """
    if elem is None:
        return {}
    out: dict[str, Any] = {}
    sz = elem.attrib.get("sz")
    if sz:
        try:
            out["size"] = float(sz) / 100.0
        except ValueError:
            pass
    bold = elem.attrib.get("b")
    if bold is not None:
        out["weight"] = "bold" if _bool_attr(bold) else "normal"
    italic = elem.attrib.get("i")
    if italic is not None:
        out["italic"] = _bool_attr(italic)
    underline = elem.attrib.get("u")
    if underline is not None:
        out["underline"] = UNDERLINE_MAP.get(underline, "single")
    latin = elem.find(f"{A_NS}latin")
    if latin is not None:
        typeface = latin.attrib.get("typeface")
        if typeface:
            resolved, is_theme = resolve_theme_font(typeface, theme)
            if resolved:
                out["family"] = resolved
                if is_theme:
                    out["familySource"] = "themeFont"
    color = _color_from_props(elem, theme)
    if color:
        out["color"] = color
    return out


def levels_from_style_elem(
    style_elem: ET.Element | None, theme: dict[str, Any]
) -> dict[int, dict[str, Any]]:
    """Parse `<a:lvl1pPr>`..`<a:lvl9pPr>` into a {level_index: props} map.

    Works for `<a:lstStyle>` (shape / layout / master placeholder list styles),
    `<p:titleStyle|bodyStyle|otherStyle>`, and `<p:defaultTextStyle>` — all
    carry the same `lvlNpPr` children.
    """
    out: dict[int, dict[str, Any]] = {}
    if style_elem is None:
        return out
    for level in range(1, 10):
        lvl = style_elem.find(f"{A_NS}lvl{level}pPr")
        if lvl is None:
            continue
        props = extract_paragraph_props(lvl, theme)
        if props:
            out[level - 1] = props
    return out


def _at_level(levels: dict[int, dict[str, Any]], n: int) -> dict[str, Any]:
    """Pick the props for indent level n, falling back to the nearest lower
    defined level, then 0. PowerPoint clamps unknown levels this way.
    """
    if n in levels:
        return levels[n]
    for candidate in range(n - 1, -1, -1):
        if candidate in levels:
            return levels[candidate]
    return {}


def _bullet_from_ppr(ppr: ET.Element | None) -> dict[str, Any] | None:
    if ppr is None:
        return None
    if ppr.find(f"{A_NS}buNone") is not None:
        return {"kind": "none", "char": None, "numberFormat": None}
    bu_char = ppr.find(f"{A_NS}buChar")
    if bu_char is not None:
        return {"kind": "char", "char": bu_char.attrib.get("char"), "numberFormat": None}
    bu_auto = ppr.find(f"{A_NS}buAutoNum")
    if bu_auto is not None:
        return {"kind": "number", "char": None, "numberFormat": bu_auto.attrib.get("type")}
    return None


def _spacing_from_node(node: ET.Element | None) -> dict[str, Any] | None:
    if node is None:
        return None
    pct = node.find(f"{A_NS}spcPct")
    if pct is not None and pct.attrib.get("val") is not None:
        value = int(pct.attrib["val"])
        return {
            "kind": "percent",
            "value": value,
            "ratio": value / 100000,
            "rawXml": ET.tostring(node, encoding="unicode"),
        }
    pts = node.find(f"{A_NS}spcPts")
    if pts is not None and pts.attrib.get("val") is not None:
        value = int(pts.attrib["val"])
        return {
            "kind": "points",
            "value": value,
            "points": value / 100,
            "rawXml": ET.tostring(node, encoding="unicode"),
        }
    return None


def extract_paragraph_props(ppr: ET.Element | None, theme: dict[str, Any]) -> dict[str, Any]:
    props = extract_run_props(ppr.find(f"{A_NS}defRPr"), theme) if ppr is not None else {}
    bullet = _bullet_from_ppr(ppr)
    if bullet is not None:
        props["bullet"] = bullet
    if ppr is not None:
        if ppr.attrib.get("marL") is not None:
            props["marginLeftEmu"] = int(ppr.attrib["marL"])
        if ppr.attrib.get("indent") is not None:
            props["indentEmu"] = int(ppr.attrib["indent"])
        spacing_before = _spacing_from_node(ppr.find(f"{A_NS}spcBef"))
        spacing_after = _spacing_from_node(ppr.find(f"{A_NS}spcAft"))
        line_spacing = _spacing_from_node(ppr.find(f"{A_NS}lnSpc"))
        if spacing_before:
            props["spacingBefore"] = spacing_before
        if spacing_after:
            props["spacingAfter"] = spacing_after
        if line_spacing:
            props["lineSpacing"] = line_spacing
    return props


def _resolve_run(
    rpr: ET.Element | None,
    para_defaults: dict[str, Any],
    shape_levels: dict[int, dict[str, Any]],
    style_context: Any,
    level_n: int,
    theme: dict[str, Any],
) -> dict[str, Any]:
    """Walk the inheritance chain for one run, most specific level first."""
    candidates: list[tuple[str, dict[str, Any]]] = [
        ("run", extract_run_props(rpr, theme)),
        ("paragraph", para_defaults),
        ("shapeListStyle", _at_level(shape_levels, level_n)),
    ]
    if style_context is not None:
        candidates += [
            ("layoutPlaceholder", _at_level(style_context.layout_placeholder, level_n)),
            ("masterPlaceholder", _at_level(style_context.master_placeholder, level_n)),
            ("masterTextStyle", _at_level(style_context.master_text_style, level_n)),
            ("presentationDefault", _at_level(style_context.presentation_default, level_n)),
        ]

    font: dict[str, Any] = {}
    resolved_from: dict[str, str] = {}
    for field in FONT_FIELDS:
        for label, props in candidates:
            if props.get(field) is not None:
                font[field] = props[field]
                if field == "family" and props.get("familySource") == "themeFont":
                    resolved_from[field] = "themeFont"
                else:
                    resolved_from[field] = label
                break
        else:
            if field in FIELD_DEFAULTS:
                font[field] = FIELD_DEFAULTS[field]
                resolved_from[field] = "default"
            else:
                font[field] = None
                resolved_from[field] = "unresolved"

    color: str | None = None
    for label, props in candidates:
        if props.get("color") is not None:
            color = props["color"]
            resolved_from["color"] = label
            break
    else:
        resolved_from["color"] = "unresolved"

    return {"font": font, "color": color, "resolvedFrom": resolved_from}


def _resolve_bullet(
    direct_bullet: dict[str, Any] | None,
    shape_levels: dict[int, dict[str, Any]],
    style_context: Any,
    level_n: int,
) -> dict[str, Any] | None:
    if direct_bullet is not None:
        return direct_bullet
    candidates: list[dict[str, Any]] = [
        _at_level(shape_levels, level_n),
    ]
    if style_context is not None:
        candidates += [
            _at_level(style_context.layout_placeholder, level_n),
            _at_level(style_context.master_placeholder, level_n),
            _at_level(style_context.master_text_style, level_n),
            _at_level(style_context.presentation_default, level_n),
        ]
    for props in candidates:
        if "bullet" in props:
            return props["bullet"]
    return None


def _resolve_paragraph_metric(
    field: str,
    direct_props: dict[str, Any],
    shape_levels: dict[int, dict[str, Any]],
    style_context: Any,
    level_n: int,
) -> int | None:
    candidates: list[dict[str, Any]] = [
        direct_props,
        _at_level(shape_levels, level_n),
    ]
    if style_context is not None:
        candidates += [
            _at_level(style_context.layout_placeholder, level_n),
            _at_level(style_context.master_placeholder, level_n),
            _at_level(style_context.master_text_style, level_n),
            _at_level(style_context.presentation_default, level_n),
        ]
    for props in candidates:
        value = props.get(field)
        if value is not None:
            return int(value)
    return None


def _resolve_paragraph_value(
    field: str,
    direct_props: dict[str, Any],
    shape_levels: dict[int, dict[str, Any]],
    style_context: Any,
    level_n: int,
) -> Any:
    candidates: list[dict[str, Any]] = [
        direct_props,
        _at_level(shape_levels, level_n),
    ]
    if style_context is not None:
        candidates += [
            _at_level(style_context.layout_placeholder, level_n),
            _at_level(style_context.master_placeholder, level_n),
            _at_level(style_context.master_text_style, level_n),
            _at_level(style_context.presentation_default, level_n),
        ]
    for props in candidates:
        if props.get(field) is not None:
            return props[field]
    return None


def parse_paragraphs(
    tx_body: ET.Element | None,
    theme: dict[str, Any],
    style_context: Any = None,
) -> list[dict[str, Any]]:
    """Walk a `<p:txBody>` / `<a:txBody>` into structured paragraphs.

    `style_context` is optional: when None the resolver only sees slide-local
    levels (experiment4 behavior), which keeps the experiment4 unit tests
    valid. Production callers pass a `StyleContext` from `placeholder_resolver`.
    """
    if tx_body is None:
        return []
    shape_levels = levels_from_style_elem(tx_body.find(f"{A_NS}lstStyle"), theme)
    paragraphs: list[dict[str, Any]] = []
    for p_elem in tx_body.findall(f"{A_NS}p"):
        ppr = p_elem.find(f"{A_NS}pPr")
        direct_para_props = extract_paragraph_props(ppr, theme)
        para_defaults = extract_run_props(ppr.find(f"{A_NS}defRPr"), theme) if ppr is not None else {}
        alignment = None
        indent_level = 0
        if ppr is not None:
            algn = ppr.attrib.get("algn")
            if algn:
                alignment = ALIGNMENT_MAP.get(algn, algn)
            lvl = ppr.attrib.get("lvl")
            if lvl:
                try:
                    indent_level = int(lvl)
                except ValueError:
                    indent_level = 0
        bullet = _resolve_bullet(_bullet_from_ppr(ppr), shape_levels, style_context, indent_level)
        margin_left_emu = _resolve_paragraph_metric(
            "marginLeftEmu", direct_para_props, shape_levels, style_context, indent_level
        )
        indent_emu = _resolve_paragraph_metric(
            "indentEmu", direct_para_props, shape_levels, style_context, indent_level
        )
        spacing_before = _resolve_paragraph_value(
            "spacingBefore", direct_para_props, shape_levels, style_context, indent_level
        )
        spacing_after = _resolve_paragraph_value(
            "spacingAfter", direct_para_props, shape_levels, style_context, indent_level
        )
        line_spacing = _resolve_paragraph_value(
            "lineSpacing", direct_para_props, shape_levels, style_context, indent_level
        )
        runs: list[dict[str, Any]] = []
        end_para_style = None
        for child in list(p_elem):
            if child.tag == f"{A_NS}r":
                t_elem = child.find(f"{A_NS}t")
                text = (t_elem.text or "") if t_elem is not None else ""
                if not text:
                    continue
                run = _resolve_run(
                    child.find(f"{A_NS}rPr"),
                    para_defaults,
                    shape_levels,
                    style_context,
                    indent_level,
                    theme,
                )
                run["text"] = text
                runs.append(run)
            elif child.tag == f"{A_NS}br":
                run = _resolve_run(
                    child.find(f"{A_NS}rPr"),
                    para_defaults,
                    shape_levels,
                    style_context,
                    indent_level,
                    theme,
                )
                run["text"] = "\n"
                run["break"] = True
                runs.append(run)
            elif child.tag == f"{A_NS}endParaRPr":
                end_para_style = _resolve_run(
                    child,
                    para_defaults,
                    shape_levels,
                    style_context,
                    indent_level,
                    theme,
                )
        paragraphs.append(
            {
                "alignment": alignment,
                "indentLevel": indent_level,
                "marginLeftEmu": margin_left_emu,
                "indentEmu": indent_emu,
                "spacingBefore": spacing_before,
                "spacingAfter": spacing_after,
                "lineSpacing": line_spacing,
                "bullet": bullet,
                "runs": runs,
                "empty": not runs,
                "emptyRunStyle": end_para_style,
                "rawParagraphAttrs": dict(ppr.attrib) if ppr is not None else {},
            }
        )
    return paragraphs


def joined_text(paragraphs: list[dict[str, Any]]) -> str:
    """Recompose flat text from parsed paragraphs (used to populate block.text)."""
    lines: list[str] = []
    for paragraph in paragraphs:
        text = "".join(run.get("text", "") for run in paragraph.get("runs", []))
        if text:
            bullet = paragraph.get("bullet") or {}
            if bullet.get("kind") == "char" and bullet.get("char"):
                text = f"{bullet['char']} {text}"
            elif bullet.get("kind") == "number":
                text = f"1. {text}"
            lines.append(text)
    return "\n".join(lines)
