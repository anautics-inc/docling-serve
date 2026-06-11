"""Parse `ppt/theme/themeN.xml` into a resolved color + font scheme.

The deck-level theme is the foundation for typography color resolution. Every
`<a:schemeClr val="tx1"/>` in a run color reference must dereference through
this map, or downstream renderers can't draw text at all.
"""
from __future__ import annotations

import zipfile
from typing import Any
from xml.etree import ElementTree as ET


A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# OOXML's <a:clrScheme> uses element names like dk1/lt1. Shape color refs
# (<a:schemeClr val="tx1"/>) use the public-facing names. Map them.
SCHEME_TO_REF = {
    "dk1": "tx1",
    "lt1": "bg1",
    "dk2": "tx2",
    "lt2": "bg2",
    "accent1": "accent1",
    "accent2": "accent2",
    "accent3": "accent3",
    "accent4": "accent4",
    "accent5": "accent5",
    "accent6": "accent6",
    "hlink": "hlink",
    "folHlink": "folHlink",
}

# Office default — used when the deck's theme omits a slot.
OFFICE_DEFAULT_SCHEME = {
    "tx1": "#000000",
    "bg1": "#FFFFFF",
    "tx2": "#44546A",
    "bg2": "#E7E6E6",
    "accent1": "#5B9BD5",
    "accent2": "#ED7D31",
    "accent3": "#A5A5A5",
    "accent4": "#FFC000",
    "accent5": "#4472C4",
    "accent6": "#70AD47",
    "hlink": "#0563C1",
    "folHlink": "#954F72",
}

OFFICE_DEFAULT_FONT_SCHEME = {
    "name": "Office",
    "majorFont": {"latin": "Calibri Light", "ea": "", "cs": ""},
    "minorFont": {"latin": "Calibri", "ea": "", "cs": ""},
}


def _hex_from_color_element(parent: ET.Element | None) -> str | None:
    """Pull the resolved hex from a color-bearing element (srgbClr/sysClr).

    Theme scheme entries always end at one of these two leaves — there's no
    schemeClr inside a clrScheme to recurse into.
    """
    if parent is None:
        return None
    srgb = parent.find(f"{A_NS}srgbClr")
    if srgb is not None and srgb.attrib.get("val"):
        return f"#{srgb.attrib['val'].upper()}"
    sys_clr = parent.find(f"{A_NS}sysClr")
    if sys_clr is not None:
        last = sys_clr.attrib.get("lastClr")
        if last:
            return f"#{last.upper()}"
    return None


def parse_color_scheme(theme_root: ET.Element) -> dict[str, str]:
    scheme: dict[str, str] = dict(OFFICE_DEFAULT_SCHEME)
    clr_scheme = theme_root.find(f".//{A_NS}clrScheme")
    if clr_scheme is None:
        return scheme
    for child in list(clr_scheme):
        tag = child.tag.removeprefix(A_NS)
        ref_name = SCHEME_TO_REF.get(tag)
        if ref_name is None:
            continue
        resolved = _hex_from_color_element(child)
        if resolved:
            scheme[ref_name] = resolved
    return scheme


def _font_block(parent: ET.Element | None) -> dict[str, str]:
    if parent is None:
        return {"latin": "", "ea": "", "cs": ""}
    out = {"latin": "", "ea": "", "cs": ""}
    latin = parent.find(f"{A_NS}latin")
    if latin is not None:
        out["latin"] = latin.attrib.get("typeface", "")
    ea = parent.find(f"{A_NS}ea")
    if ea is not None:
        out["ea"] = ea.attrib.get("typeface", "")
    cs = parent.find(f"{A_NS}cs")
    if cs is not None:
        out["cs"] = cs.attrib.get("typeface", "")
    return out


def parse_font_scheme(theme_root: ET.Element) -> dict[str, Any]:
    font_scheme = theme_root.find(f".//{A_NS}fontScheme")
    if font_scheme is None:
        return dict(OFFICE_DEFAULT_FONT_SCHEME)
    return {
        "name": font_scheme.attrib.get("name") or "",
        "majorFont": _font_block(font_scheme.find(f"{A_NS}majorFont")),
        "minorFont": _font_block(font_scheme.find(f"{A_NS}minorFont")),
    }


def parse_theme(zf: zipfile.ZipFile, theme_path: str = "ppt/theme/theme1.xml") -> dict[str, Any]:
    """Return the deck-level theme.

    Always returns a complete `colorScheme` (12 slots) and `fontScheme`. Slots
    missing from the deck's actual theme fall back to Office defaults so that
    downstream renderers never see `null` colors.
    """
    if theme_path not in zf.namelist():
        # Find any theme file — some decks number them differently.
        candidates = [n for n in zf.namelist() if n.startswith("ppt/theme/") and n.endswith(".xml")]
        if not candidates:
            return {
                "themeFile": None,
                "themeName": None,
                "colorScheme": dict(OFFICE_DEFAULT_SCHEME),
                "fontScheme": dict(OFFICE_DEFAULT_FONT_SCHEME),
            }
        theme_path = sorted(candidates)[0]
    try:
        root = ET.fromstring(zf.read(theme_path))
    except ET.ParseError:
        return {
            "themeFile": theme_path,
            "themeName": None,
            "colorScheme": dict(OFFICE_DEFAULT_SCHEME),
            "fontScheme": dict(OFFICE_DEFAULT_FONT_SCHEME),
        }
    return {
        "themeFile": theme_path,
        "themeName": root.attrib.get("name") or "",
        "colorScheme": parse_color_scheme(root),
        "fontScheme": parse_font_scheme(root),
    }


def resolve_scheme_color(scheme_ref: str, theme: dict[str, Any]) -> str | None:
    """Dereference a `<a:schemeClr val="..."/>` reference against the parsed theme."""
    if not scheme_ref:
        return None
    cs = theme.get("colorScheme") or {}
    return cs.get(scheme_ref) or cs.get(SCHEME_TO_REF.get(scheme_ref, ""))
