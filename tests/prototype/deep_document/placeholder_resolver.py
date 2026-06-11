"""Build a `StyleContext` for each placeholder on a slide.

The context carries inheritance levels 4–7 (layout placeholder, master
placeholder, master text style, presentation default). `typography.parse_paragraphs`
consumes it to fill any run property the slide-local levels (1–3) left null.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .ooxml import A_NS, P_NS, normalize_part_target, read_xml, rels_for
from .text_styles import parse_master_text_styles, text_style_key
from .typography import levels_from_style_elem


@dataclass
class StyleContext:
    """Inheritance levels 4–7 for one placeholder identity.

    Each map is {indent_level_index: props}. Empty maps are fine — the
    resolver simply skips a level that contributes nothing.
    """

    layout_placeholder: dict[int, dict[str, Any]] = field(default_factory=dict)
    master_placeholder: dict[int, dict[str, Any]] = field(default_factory=dict)
    master_text_style: dict[int, dict[str, Any]] = field(default_factory=dict)
    presentation_default: dict[int, dict[str, Any]] = field(default_factory=dict)


def _placeholder_ref(shape: ET.Element) -> tuple[str | None, str | None]:
    ph = shape.find(f".//{P_NS}ph")
    if ph is None:
        return (None, None)
    return (ph.attrib.get("type"), ph.attrib.get("idx"))


def _shapes_in(root: ET.Element | None) -> list[ET.Element]:
    if root is None:
        return []
    sp_tree = root.find(f".//{P_NS}spTree")
    if sp_tree is None:
        return []
    return [child for child in sp_tree if child.tag == f"{P_NS}sp"]


def _lststyle_levels(shape: ET.Element, theme: dict[str, Any]) -> dict[int, dict[str, Any]]:
    tx_body = shape.find(f"{P_NS}txBody")
    if tx_body is None:
        return {}
    return levels_from_style_elem(tx_body.find(f"{A_NS}lstStyle"), theme)


def _norm_type(ph_type: str | None) -> str:
    """title and ctrTitle are interchangeable for placeholder matching."""
    if ph_type in {"title", "ctrTitle"}:
        return "title"
    return ph_type or "body"


def _match_placeholder(
    root: ET.Element | None, ph_type: str | None, ph_idx: str | None
) -> ET.Element | None:
    """Find the placeholder shape in a layout/master spTree.

    Match priority: exact `idx` match, then `type` match (title~ctrTitle
    treated as equal).
    """
    candidates = _shapes_in(root)
    if ph_idx is not None:
        for shape in candidates:
            _, idx = _placeholder_ref(shape)
            if idx == ph_idx:
                return shape
    want = _norm_type(ph_type)
    for shape in candidates:
        shape_type, _ = _placeholder_ref(shape)
        if _norm_type(shape_type) == want:
            return shape
    return None


class SlideStyleResolver:
    """Resolves StyleContexts for all placeholders on one slide.

    Layout/master parts are resolved once per slide. The master text-style
    parse is memoized in the shared `master_cache` so a 250-slide deck parses
    its master once, not 250 times.
    """

    def __init__(
        self,
        zf: zipfile.ZipFile,
        slide_part: str,
        slide_rels: dict[str, dict[str, str]],
        theme: dict[str, Any],
        master_cache: dict[str, dict[str, dict[int, dict[str, Any]]]],
        presentation_default: dict[int, dict[str, Any]],
    ) -> None:
        self.theme = theme
        self.presentation_default = presentation_default
        self._context_cache: dict[tuple[str | None, str | None], StyleContext] = {}
        self.layout_root: ET.Element | None = None
        self.master_root: ET.Element | None = None
        self.master_text_styles: dict[str, dict[int, dict[str, Any]]] = {
            "title": {},
            "body": {},
            "other": {},
        }

        layout_part = self._part_of(slide_part, slide_rels, "/slideLayout")
        if layout_part is None:
            return
        self.layout_root = read_xml(zf, layout_part)
        layout_rels = rels_for(zf, layout_part)
        master_part = self._part_of(layout_part, layout_rels, "/slideMaster")
        if master_part is None:
            return
        self.master_root = read_xml(zf, master_part)
        if master_part not in master_cache:
            master_cache[master_part] = parse_master_text_styles(zf, master_part, theme)
        self.master_text_styles = master_cache[master_part]

    @staticmethod
    def _part_of(
        source_part: str, rels: dict[str, dict[str, str]], suffix: str
    ) -> str | None:
        for rel in rels.values():
            if rel["type"].endswith(suffix):
                return normalize_part_target(source_part, rel["target"])
        return None

    def context_for(self, ph_type: str | None, ph_idx: str | None) -> StyleContext:
        """Return the StyleContext for a shape with the given placeholder identity.

        Shapes with no placeholder (`ph_type is None`) resolve to the master's
        `otherStyle` and contribute no layout/master placeholder levels.
        """
        key = (ph_type, ph_idx)
        cached = self._context_cache.get(key)
        if cached is not None:
            return cached

        ctx = StyleContext(presentation_default=self.presentation_default)
        ctx.master_text_style = self.master_text_styles.get(text_style_key(ph_type), {})

        layout_ph = _match_placeholder(self.layout_root, ph_type, ph_idx)
        master_match_type = ph_type
        if layout_ph is not None:
            ctx.layout_placeholder = _lststyle_levels(layout_ph, self.theme)
            layout_ph_type, _ = _placeholder_ref(layout_ph)
            if layout_ph_type:
                master_match_type = layout_ph_type

        master_ph = _match_placeholder(self.master_root, master_match_type, None)
        if master_ph is not None:
            ctx.master_placeholder = _lststyle_levels(master_ph, self.theme)

        self._context_cache[key] = ctx
        return ctx


def build_slide_resolver(
    zf: zipfile.ZipFile,
    slide_part: str,
    slide_rels: dict[str, dict[str, str]],
    theme: dict[str, Any],
    master_cache: dict[str, dict[str, dict[int, dict[str, Any]]]],
    presentation_default: dict[int, dict[str, Any]],
) -> SlideStyleResolver:
    return SlideStyleResolver(
        zf, slide_part, slide_rels, theme, master_cache, presentation_default
    )
