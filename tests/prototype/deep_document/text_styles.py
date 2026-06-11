"""Parse a slide master's `<p:txStyles>` and `presentation.xml`'s
`<p:defaultTextStyle>` — inheritance levels 6 and 7.

These are the bottom of the typography chain. Most slide decks state font
size/color only here (in the master) and inherit it everywhere else, which is
why experiment4 measured `runs_with_size` as low as 7%.
"""
from __future__ import annotations

import zipfile
from typing import Any

from .ooxml import read_xml
from .typography import levels_from_style_elem


P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

# Placeholder type → which master text style applies.
TITLE_TYPES = {"title", "ctrTitle"}
BODY_TYPES = {"body", "subTitle", "obj", "tx"}


def text_style_key(placeholder_type: str | None) -> str:
    """Map a placeholder type to title|body|other."""
    if placeholder_type in TITLE_TYPES:
        return "title"
    if placeholder_type in BODY_TYPES:
        return "body"
    return "other"


def parse_master_text_styles(
    zf: zipfile.ZipFile, master_part: str, theme: dict[str, Any]
) -> dict[str, dict[int, dict[str, Any]]]:
    """Return {'title': {level: props}, 'body': {...}, 'other': {...}}.

    Empty maps when the master omits a style tree — callers treat a missing
    level as "nothing to contribute," which the resolver handles gracefully.
    """
    empty: dict[str, dict[int, dict[str, Any]]] = {"title": {}, "body": {}, "other": {}}
    root = read_xml(zf, master_part)
    if root is None:
        return empty
    tx_styles = root.find(f".//{P_NS}txStyles")
    if tx_styles is None:
        return empty
    return {
        "title": levels_from_style_elem(tx_styles.find(f"{P_NS}titleStyle"), theme),
        "body": levels_from_style_elem(tx_styles.find(f"{P_NS}bodyStyle"), theme),
        "other": levels_from_style_elem(tx_styles.find(f"{P_NS}otherStyle"), theme),
    }


def parse_presentation_default_style(
    zf: zipfile.ZipFile, theme: dict[str, Any]
) -> dict[int, dict[str, Any]]:
    """Return {level: props} from `presentation.xml`'s `<p:defaultTextStyle>`."""
    root = read_xml(zf, "ppt/presentation.xml")
    if root is None:
        return {}
    return levels_from_style_elem(root.find(f"{P_NS}defaultTextStyle"), theme)
