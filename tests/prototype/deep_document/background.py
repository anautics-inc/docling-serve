"""Resolve a slide's effective background by walking slide → layout → master.

The first non-empty `<p:bg>` along that walk wins. Image backgrounds are
captured as `manifest.assets[]` entries with `kind="slide_background"`; their
binary is deduped by sha256 like every other asset.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .ooxml import (
    A_NS,
    P_NS,
    R_NS,
    MediaAsset,
    _mime_for,
    normalize_part_target,
    read_xml,
    rels_for,
    sha256_bytes,
)
from .theme import resolve_scheme_color


def _solid_color(bg_pr: ET.Element, theme: dict[str, Any]) -> str | None:
    solid = bg_pr.find(f"{A_NS}solidFill")
    if solid is None:
        return None
    srgb = solid.find(f"{A_NS}srgbClr")
    if srgb is not None and srgb.attrib.get("val"):
        return f"#{srgb.attrib['val'].upper()}"
    sys_clr = solid.find(f"{A_NS}sysClr")
    if sys_clr is not None and sys_clr.attrib.get("lastClr"):
        return f"#{sys_clr.attrib['lastClr'].upper()}"
    scheme = solid.find(f"{A_NS}schemeClr")
    if scheme is not None and scheme.attrib.get("val"):
        return resolve_scheme_color(scheme.attrib["val"], theme)
    return None


def _image_rel_id(bg_pr: ET.Element) -> str | None:
    blip_fill = bg_pr.find(f"{A_NS}blipFill")
    if blip_fill is None:
        return None
    blip = blip_fill.find(f"{A_NS}blip")
    if blip is None:
        return None
    return blip.attrib.get(f"{R_NS}embed") or blip.attrib.get(f"{R_NS}link")


def _is_gradient(bg_pr: ET.Element) -> bool:
    return bg_pr.find(f"{A_NS}gradFill") is not None


def _bg_ref_color(bg_ref: ET.Element, theme: dict[str, Any]) -> str | None:
    scheme = bg_ref.find(f"{A_NS}schemeClr")
    if scheme is not None and scheme.attrib.get("val"):
        return resolve_scheme_color(scheme.attrib["val"], theme)
    srgb = bg_ref.find(f"{A_NS}srgbClr")
    if srgb is not None and srgb.attrib.get("val"):
        return f"#{srgb.attrib['val'].upper()}"
    return None


def _resolve_one(
    zf: zipfile.ZipFile,
    part_name: str,
    theme: dict[str, Any],
    assets: dict[str, MediaAsset],
    source_label: str,
) -> dict[str, Any] | None:
    """Inspect a single slide/layout/master part for a `<p:bg>` declaration.

    Returns a background dict if the part declares one explicitly; None if
    the part inherits from its parent or has no background at all.
    """
    root = read_xml(zf, part_name)
    if root is None:
        return None
    bg = root.find(f".//{P_NS}bg")
    if bg is None:
        return None
    bg_pr = bg.find(f"{P_NS}bgPr")
    bg_ref = bg.find(f"{P_NS}bgRef")

    if bg_pr is not None:
        solid = _solid_color(bg_pr, theme)
        if solid:
            return {"kind": "solid", "color": solid, "assetId": None, "source": source_label}
        if _is_gradient(bg_pr):
            return {"kind": "gradient", "color": None, "assetId": None, "source": source_label}
        rel_id = _image_rel_id(bg_pr)
        if rel_id:
            asset_id = _register_image_asset(zf, part_name, rel_id, assets)
            if asset_id:
                return {"kind": "image", "color": None, "assetId": asset_id, "source": source_label}

    if bg_ref is not None:
        color = _bg_ref_color(bg_ref, theme)
        if color:
            return {"kind": "solid", "color": color, "assetId": None, "source": source_label}

    return None


def _register_image_asset(
    zf: zipfile.ZipFile,
    source_part: str,
    rel_id: str,
    assets: dict[str, MediaAsset],
) -> str | None:
    """Add the background image binary to the asset registry, deduped by sha."""
    rels = rels_for(zf, source_part)
    rel = rels.get(rel_id)
    if not rel:
        return None
    target = normalize_part_target(source_part, rel["target"])
    if target not in zf.namelist():
        return None
    binary = zf.read(target)
    sha = sha256_bytes(binary)
    asset_id = f"asset-{sha[:8]}"
    asset = assets.get(asset_id)
    if asset is None:
        suffix = Path(target).suffix.lower() or ".bin"
        mime = _mime_for(target)
        asset = MediaAsset(
            asset_id=asset_id,
            kind="slide_background" if mime.startswith("image/") else "binary",
            mime_type=mime,
            size_bytes=len(binary),
            sha256=sha,
            extension=suffix,
            binary=binary,
        )
        assets[asset_id] = asset
    # Even if an existing asset_id collides with a foreground picture, we
    # don't clobber its kind — but we do mark that this binary doubled as
    # background context via source_parts.
    asset.source_parts.add(target)
    return asset_id


def slide_background(
    zf: zipfile.ZipFile,
    slide_part: str,
    slide_rels: dict[str, dict[str, str]],
    theme: dict[str, Any],
    assets: dict[str, MediaAsset],
) -> dict[str, Any]:
    """Resolve the effective background for one slide.

    Walk: slide → layout → master. Returns the first explicit `<p:bg>` found.
    If nothing is explicit anywhere in the chain, returns
    `{kind:"none", source:"master"}` so renderers default to white.
    """
    # 1. Slide itself
    found = _resolve_one(zf, slide_part, theme, assets, source_label="slide")
    if found:
        return found

    # 2. Layout (resolved via slide rels)
    layout_part: str | None = None
    for rel in slide_rels.values():
        if rel["type"].endswith("/slideLayout"):
            layout_part = normalize_part_target(slide_part, rel["target"])
            break
    if layout_part:
        layout_rels = rels_for(zf, layout_part)
        found = _resolve_one(zf, layout_part, theme, assets, source_label="layout")
        if found:
            return found

        # 3. Master (resolved via layout rels)
        master_part: str | None = None
        for rel in layout_rels.values():
            if rel["type"].endswith("/slideMaster"):
                master_part = normalize_part_target(layout_part, rel["target"])
                break
        if master_part:
            found = _resolve_one(zf, master_part, theme, assets, source_label="master")
            if found:
                return found

    return {"kind": "none", "color": None, "assetId": None, "source": "master"}
