"""Render a slide unit to a self-contained SVG preview.

A deck's structured ``document.json`` already carries every slide as absolutely-positioned geometry
(``unit.render`` + ``unit.content.elements`` with ``bbox`` / ``text`` / ``assetRef``) — the SAME
shape the notebook deck surface renders. Rather than rasterize the original ``.pptx`` with
LibreOffice on view (slow, stateful, ``/tmp``-bound), we render one ``media/slide-{n}.svg`` per
slide from that geometry at extraction time and ship it in the bundle. The SVG is deterministic,
tiny, vector, and matches the surface because both derive from the same elements.

Images are embedded as ``data:`` URIs so the SVG is self-contained and renders even when loaded as
an ``<img src>`` (where external references are blocked). Text is rendered as ``<text>`` lines —
approximate wrapping, which is more than enough for a thumbnail/reference preview.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

# CSS px per point at 96 DPI — matches the workbench deck template (``PX_PER_PT``).
PX_PER_PT = 96.0 / 72.0
_DEFAULT_SIZE = (960.0, 720.0)
_DEFAULT_TEXT_COLOR = "#222222"
_DEFAULT_FONT_PX = 18.0
_FONT_FAMILY = (
    "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
)


def write_slide_svgs(units: list[dict[str, Any]], *, media_dir: Path) -> list[str]:
    """Render each slide unit to ``media/slide-{n}.svg``; returns the relative bundle paths.

    Non-slide units (or units without geometry) are skipped. Never raises — a single bad slide is
    logged and skipped so it can't fail the whole extraction.
    """
    if not units:
        return []
    media_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        number = _slide_number(unit, index)
        try:
            svg = render_slide_svg(unit, media_dir=media_dir)
        except Exception:
            # One bad slide must not fail the whole bundle.
            logger.warning("failed to render slide SVG for unit %s", unit.get("unitId"))
            continue
        if not svg:
            continue
        # Non-padded `slide-{n}.svg` — the bundle contract (ADR 0002) and the
        # workbench deck surface both address slides by 1-based page number
        # (`media/slide-${page}.svg`), so the filename must not be zero-padded.
        name = f"slide-{number}.svg"
        (media_dir / name).write_text(svg, encoding="utf-8")
        written.append(f"media/{name}")
    return written


def render_slide_svg(unit: dict[str, Any], *, media_dir: Path) -> str:
    """Render one slide unit's geometry to an SVG string."""
    width, height = _slide_size(unit)
    background = _background_color(unit)
    elements = _elements(unit)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_fmt(width)}" height="{_fmt(height)}" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" '
        f'font-family="{escape(_FONT_FAMILY)}">',
        f'<rect x="0" y="0" width="{_fmt(width)}" height="{_fmt(height)}" '
        f'fill="{_color(background, "#FFFFFF")}"/>',
    ]

    for element in sorted(elements, key=lambda e: _z(e)):
        kind = str(element.get("type") or "")
        bbox = element.get("bbox") or {}
        if kind == "image" and element.get("assetRef"):
            parts.append(_image_svg(element, bbox, media_dir=media_dir))
        elif kind in {"text", "table"}:
            parts.append(_text_svg(element, bbox))

    parts.append("</svg>")
    return "".join(part for part in parts if part)


def _image_svg(element: dict[str, Any], bbox: dict[str, Any], *, media_dir: Path) -> str:
    x, y, w, h = _box(bbox)
    if w <= 0 or h <= 0:
        return ""
    data_uri = _image_data_uri(media_dir, str(element.get("assetRef") or ""))
    if not data_uri:
        return ""
    return (
        f'<image x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(w)}" height="{_fmt(h)}" '
        f'preserveAspectRatio="xMidYMid meet" href="{data_uri}"/>'
    )


def _text_svg(element: dict[str, Any], bbox: dict[str, Any]) -> str:
    x, y, _w, h = _box(bbox)
    text = element.get("text") or {}
    paragraphs = text.get("paragraphs")
    lines: list[tuple[str, float, str, bool]] = []  # (text, fontPx, color, bold)
    if isinstance(paragraphs, list) and paragraphs:
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                continue
            line = str(paragraph.get("text") or "").strip()
            if not line:
                continue
            lines.append(
                (
                    line,
                    _paragraph_font_px(paragraph),
                    _paragraph_color(paragraph),
                    _paragraph_bold(paragraph),
                )
            )
    else:
        plain = str(text.get("plain") or "").strip()
        for line in plain.splitlines():
            if line.strip():
                lines.append((line.strip(), _DEFAULT_FONT_PX, _DEFAULT_TEXT_COLOR, False))
    if not lines:
        return ""

    # Stack lines from the top of the box; advance by the line's own height.
    spans: list[str] = []
    cursor_y = y
    for line, font_px, color, bold in lines:
        cursor_y += font_px
        if cursor_y > y + h + font_px:  # overflowed the box; stop (thumbnail)
            break
        weight = ' font-weight="700"' if bold else ""
        spans.append(
            f'<text x="{_fmt(x)}" y="{_fmt(cursor_y)}" '
            f'font-size="{_fmt(font_px)}" fill="{color}"{weight}>{escape(line)}</text>'
        )
        cursor_y += font_px * 0.35
    return "".join(spans)


def _image_data_uri(media_dir: Path, asset_ref: str) -> str | None:
    if not asset_ref:
        return None
    path = media_dir / Path(asset_ref).name
    if not path.is_file():
        return None
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(blob).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _elements(unit: dict[str, Any]) -> list[dict[str, Any]]:
    content = unit.get("content")
    if isinstance(content, dict) and isinstance(content.get("elements"), list):
        return [e for e in content["elements"] if isinstance(e, dict)]
    raw = unit.get("elements")
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _slide_size(unit: dict[str, Any]) -> tuple[float, float]:
    px = (((unit.get("render") or {}).get("size") or {}).get("px")) or {}
    width = _to_float(px.get("width")) or _DEFAULT_SIZE[0]
    height = _to_float(px.get("height")) or _DEFAULT_SIZE[1]
    return width, height


def _background_color(unit: dict[str, Any]) -> str | None:
    background = (unit.get("render") or {}).get("background") or {}
    value = background.get("color")
    return str(value) if value else None


def _box(bbox: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        _to_float(bbox.get("x")) or 0.0,
        _to_float(bbox.get("y")) or 0.0,
        _to_float(bbox.get("w")) or 0.0,
        _to_float(bbox.get("h")) or 0.0,
    )


def _z(element: dict[str, Any]) -> float:
    return _to_float(element.get("zIndex")) or 0.0


def _paragraph_font_px(paragraph: dict[str, Any]) -> float:
    size_pt = _to_float(paragraph.get("sizePt"))
    if not size_pt:
        for run in paragraph.get("runs") or []:
            if isinstance(run, dict):
                size_pt = _to_float(run.get("sizePt"))
                if size_pt:
                    break
    return round(size_pt * PX_PER_PT, 2) if size_pt else _DEFAULT_FONT_PX


def _paragraph_color(paragraph: dict[str, Any]) -> str:
    for run in paragraph.get("runs") or []:
        if isinstance(run, dict) and run.get("color"):
            return _color(str(run["color"]), _DEFAULT_TEXT_COLOR)
    return _DEFAULT_TEXT_COLOR


def _paragraph_bold(paragraph: dict[str, Any]) -> bool:
    runs = [r for r in (paragraph.get("runs") or []) if isinstance(r, dict)]
    return bool(runs) and all(bool(r.get("bold")) for r in runs)


def _slide_number(unit: dict[str, Any], index: int) -> int:
    for key in ("slideNumber", "unitNumber"):
        value = unit.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return index + 1


def _color(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    candidate = value.strip()
    # Accept ``#RGB`` / ``#RRGGBB`` and bare hex; reject anything with quotes/brackets.
    if any(ch in candidate for ch in '"<>'):
        return fallback
    return candidate


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")
