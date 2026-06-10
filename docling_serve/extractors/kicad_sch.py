"""Convert exported schematic SVG geometry into a KiCad schematic (.kicad_sch).

The schematic extractor exports each PDF page to SVG with ``pdftocairo`` — a
lossless dump of the drawing's vector geometry where even text is rendered as
glyph outline paths (schematic PDFs routinely use custom font encodings, so
glyph outlines are the only faithful representation of labels). This module
replays that geometry into a KiCad 8 schematic file (``.kicad_sch``) so the
drawing — every wire, shape, and text outline — opens directly in KiCad's
schematic editor.

This is a pure, deterministic serializer: no model calls, no symbol inference.
Lines and curves become graphical ``polyline`` items (cubic/quadratic Béziers
are flattened to short segments); ``<use>`` glyph references are expanded from
the SVG ``<defs>`` table. Stroke widths and colors are preserved, scaled from
PDF points to millimetres.
"""

from __future__ import annotations

import math
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from defusedxml.common import DefusedXmlException

# Parse SVG with defusedxml to block entity-expansion / external-entity attacks.
# ``ET`` is retained for Element type hints and ``ParseError``.
from defusedxml.ElementTree import fromstring as _safe_fromstring

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

#: SVG user units from pdftocairo are PDF points (1/72 inch).
PT_TO_MM = 25.4 / 72.0

#: KiCad 8.0 schematic file format version.
KICAD_SCH_VERSION = 20231120

#: Stroke width (mm) for paths that carry no usable stroke width of their own.
DEFAULT_STROKE_WIDTH_MM = 0.15

#: Flattening tolerance for Bézier curves, in SVG units (points).
CURVE_TOLERANCE_PT = 0.1

#: Hard cap on line segments produced per Bézier curve.
MAX_CURVE_SEGMENTS = 16

#: 2D affine transform as the SVG matrix(a, b, c, d, e, f) coefficient tuple.
Matrix = tuple[float, float, float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_NUMBER_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate)\s*\(([^)]*)\)")
_PATH_COMMAND_RE = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)")


class KicadConversionError(ValueError):
    """Raised when an SVG document cannot be converted to a KiCad schematic."""


@dataclass(slots=True)
class _Shape:
    """One drawable polyline in page coordinates (SVG points, y-down)."""

    points: list[tuple[float, float]]
    closed: bool = False
    filled: bool = False
    stroke_width_pt: float | None = None
    color: tuple[int, int, int] | None = None


@dataclass(slots=True)
class _SvgGeometry:
    width_pt: float
    height_pt: float
    shapes: list[_Shape] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Transforms                                                                   #
# --------------------------------------------------------------------------- #


def _compose(parent: Matrix, child: Matrix) -> Matrix:
    """Return ``parent @ child`` (child applied first, parent second)."""
    a1, b1, c1, d1, e1, f1 = parent
    a2, b2, c2, d2, e2, f2 = child
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _scale_factor(m: Matrix) -> float:
    """Average linear scale of the transform (for scaling stroke widths)."""
    det = abs(m[0] * m[3] - m[1] * m[2])
    return math.sqrt(det) if det > 0 else 1.0


def _parse_transform(text: str | None) -> Matrix:
    matrix = IDENTITY
    if not text:
        return matrix
    for kind, args_text in _TRANSFORM_RE.findall(text):
        args = [float(v) for v in _NUMBER_RE.findall(args_text)]
        if kind == "matrix" and len(args) == 6:
            step: Matrix = (args[0], args[1], args[2], args[3], args[4], args[5])
        elif kind == "translate" and args:
            tx = args[0]
            ty = args[1] if len(args) > 1 else 0.0
            step = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif kind == "scale" and args:
            sx = args[0]
            sy = args[1] if len(args) > 1 else sx
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif kind == "rotate" and args:
            angle = math.radians(args[0])
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            step = (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                step = _compose(
                    _compose((1, 0, 0, 1, cx, cy), step), (1, 0, 0, 1, -cx, -cy)
                )
        else:
            continue
        matrix = _compose(matrix, step)
    return matrix


# --------------------------------------------------------------------------- #
# Path data                                                                    #
# --------------------------------------------------------------------------- #


def _flatten_cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> list[tuple[float, float]]:
    """Approximate a cubic Bézier with line segments (excludes ``p0``)."""
    net_length = (
        math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
    )
    segments = max(2, min(MAX_CURVE_SEGMENTS, math.ceil(math.sqrt(net_length / CURVE_TOLERANCE_PT))))
    points: list[tuple[float, float]] = []
    for step in range(1, segments + 1):
        t = step / segments
        mt = 1.0 - t
        x = (
            mt * mt * mt * p0[0]
            + 3 * mt * mt * t * p1[0]
            + 3 * mt * t * t * p2[0]
            + t * t * t * p3[0]
        )
        y = (
            mt * mt * mt * p0[1]
            + 3 * mt * mt * t * p1[1]
            + 3 * mt * t * t * p2[1]
            + t * t * t * p3[1]
        )
        points.append((x, y))
    return points


def _reflect(
    pos: tuple[float, float], ctrl: tuple[float, float] | None
) -> tuple[float, float]:
    """Reflect ``ctrl`` about ``pos`` for SVG smooth-curve commands (S/T)."""
    if ctrl is None:
        return pos
    return (2 * pos[0] - ctrl[0], 2 * pos[1] - ctrl[1])


def _quadratic_to_cubic(
    pos: tuple[float, float], q1: tuple[float, float], end: tuple[float, float]
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Control points of the cubic equivalent of a quadratic Bézier."""
    c1 = (pos[0] + 2.0 / 3.0 * (q1[0] - pos[0]), pos[1] + 2.0 / 3.0 * (q1[1] - pos[1]))
    c2 = (end[0] + 2.0 / 3.0 * (q1[0] - end[0]), end[1] + 2.0 / 3.0 * (q1[1] - end[1]))
    return c1, c2


def _trace_curve(
    op: str,
    args: list[float],
    pos: tuple[float, float],
    offset: tuple[float, float],
    prev_cubic: tuple[float, float] | None,
    prev_quad: tuple[float, float] | None,
) -> tuple[
    list[tuple[float, float]],
    tuple[float, float],
    tuple[float, float] | None,
    tuple[float, float] | None,
]:
    """Flatten one C/S/Q/T command; returns (points, end, prev_cubic, prev_quad)."""
    ox, oy = offset
    if op == "C":
        x1, y1, x2, y2, x, y = args
        c1, c2, end = (ox + x1, oy + y1), (ox + x2, oy + y2), (ox + x, oy + y)
        return _flatten_cubic(pos, c1, c2, end), end, c2, None
    if op == "S":
        x2, y2, x, y = args
        c1 = _reflect(pos, prev_cubic)
        c2, end = (ox + x2, oy + y2), (ox + x, oy + y)
        return _flatten_cubic(pos, c1, c2, end), end, c2, None
    if op == "Q":
        x1, y1, x, y = args
        q1, end = (ox + x1, oy + y1), (ox + x, oy + y)
    else:  # T
        x, y = args
        q1, end = _reflect(pos, prev_quad), (ox + x, oy + y)
    c1, c2 = _quadratic_to_cubic(pos, q1, end)
    return _flatten_cubic(pos, c1, c2, end), end, None, q1


#: Coordinate count consumed by each curve command.
_CURVE_ARITY = {"C": 6, "S": 4, "Q": 4, "T": 2}


def parse_path_data(d: str) -> list[tuple[list[tuple[float, float]], bool]]:
    """Parse an SVG path ``d`` string into flattened subpaths.

    Returns ``(points, closed)`` tuples in path-local coordinates. Cubic and
    quadratic Béziers are flattened; elliptical arcs degrade to a straight
    line to the endpoint (pdftocairo never emits them).
    """
    tokens: list[str] = []
    for command, number in _PATH_COMMAND_RE.findall(d):
        tokens.append(command or number)

    subpaths: list[tuple[list[tuple[float, float]], bool]] = []
    current: list[tuple[float, float]] = []
    pos = (0.0, 0.0)
    start = (0.0, 0.0)
    prev_cubic_ctrl: tuple[float, float] | None = None
    prev_quad_ctrl: tuple[float, float] | None = None
    command = ""
    index = 0

    def read(count: int) -> list[float]:
        nonlocal index
        values = [float(tokens[index + offset]) for offset in range(count)]
        index += count
        return values

    def finish(closed: bool) -> None:
        nonlocal current
        if len(current) >= 2:
            subpaths.append((current, closed))
        current = []

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            if command in "Zz":
                pos = start
                finish(closed=True)
                prev_cubic_ctrl = prev_quad_ctrl = None
                continue
        elif not command:
            raise KicadConversionError(f"Path data starts with a number: {d[:40]!r}")

        relative = command.islower()
        op = command.upper()
        ox, oy = pos if relative else (0.0, 0.0)

        if op == "M":
            x, y = read(2)
            finish(closed=False)
            pos = start = (ox + x, oy + y)
            current = [pos]
            # Subsequent coordinate pairs are implicit line-tos.
            command = "l" if relative else "L"
            prev_cubic_ctrl = prev_quad_ctrl = None
        elif op == "L":
            x, y = read(2)
            pos = (ox + x, oy + y)
            current.append(pos)
            prev_cubic_ctrl = prev_quad_ctrl = None
        elif op == "H":
            (x,) = read(1)
            pos = (ox + x, pos[1])
            current.append(pos)
            prev_cubic_ctrl = prev_quad_ctrl = None
        elif op == "V":
            (y,) = read(1)
            pos = (pos[0], oy + y)
            current.append(pos)
            prev_cubic_ctrl = prev_quad_ctrl = None
        elif op in _CURVE_ARITY:
            points, pos, prev_cubic_ctrl, prev_quad_ctrl = _trace_curve(
                op, read(_CURVE_ARITY[op]), pos, (ox, oy), prev_cubic_ctrl, prev_quad_ctrl
            )
            current.extend(points)
        elif op == "A":
            _rx, _ry, _rot, _large, _sweep, x, y = read(7)
            pos = (ox + x, oy + y)
            current.append(pos)
            prev_cubic_ctrl = prev_quad_ctrl = None
        else:  # pragma: no cover - regex restricts the command set
            raise KicadConversionError(f"Unsupported path command {command!r}")

    finish(closed=False)
    return subpaths


# --------------------------------------------------------------------------- #
# SVG document walk                                                            #
# --------------------------------------------------------------------------- #


def _parse_color(text: str | None) -> tuple[int, int, int] | None:
    """Parse ``rgb(R%, G%, B%)`` / ``rgb(R, G, B)`` / ``#rrggbb`` colors."""
    if not text or text == "none":
        return None
    text = text.strip()
    if text.startswith("#") and len(text) == 7:
        return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
    match = re.match(r"rgb\(([^)]*)\)", text)
    if not match:
        return None
    channels: list[int] = []
    for part in match.group(1).split(","):
        part = part.strip()
        if part.endswith("%"):
            channels.append(round(float(part[:-1]) * 255.0 / 100.0))
        else:
            channels.append(round(float(part)))
    if len(channels) != 3:
        return None
    return (channels[0], channels[1], channels[2])


def _localname(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _collect_defs(root: ET.Element) -> dict[str, ET.Element]:
    """Index every ``id``-carrying element under ``<defs>`` for ``<use>``."""
    table: dict[str, ET.Element] = {}
    for defs in root.iter(f"{SVG_NS}defs"):
        for element in defs.iter():
            identifier = element.get("id")
            if identifier:
                table[identifier] = element
    return table


def _is_page_background(
    points: list[tuple[float, float]], width: float, height: float
) -> bool:
    """True when a filled shape covers (almost) the whole page."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (
        (max(xs) - min(xs)) >= 0.95 * width
        and (max(ys) - min(ys)) >= 0.95 * height
    )


def _page_dimensions(root: ET.Element) -> tuple[float, float]:
    view_box = root.get("viewBox")
    if view_box:
        parts = [float(v) for v in _NUMBER_RE.findall(view_box)]
        width, height = parts[2], parts[3]
    else:
        width_match = _NUMBER_RE.search(root.get("width", "0"))
        height_match = _NUMBER_RE.search(root.get("height", "0"))
        width = float(width_match.group()) if width_match else 0.0
        height = float(height_match.group()) if height_match else 0.0
    if width <= 0 or height <= 0:
        raise KicadConversionError("SVG has no usable page dimensions")
    return width, height


class _GeometryWalker:
    """Recursive SVG tree walk that accumulates page-space shapes."""

    _SKIPPED_TAGS = frozenset({"defs", "clipPath", "mask", "symbol", "metadata", "style"})

    def __init__(self, defs_table: dict[str, ET.Element], geometry: _SvgGeometry) -> None:
        self._defs = defs_table
        self._geometry = geometry

    def walk(self, element: ET.Element, ctm: Matrix, inherited_fill: str | None) -> None:
        name = _localname(element)
        if name in self._SKIPPED_TAGS:
            return
        matrix = _compose(ctm, _parse_transform(element.get("transform")))
        fill = element.get("fill", inherited_fill)
        if name == "path":
            self._emit_path(element, ctm, inherited_fill)
        elif name == "use":
            self._expand_use(element, matrix, fill)
        else:
            for child in element:
                self.walk(child, matrix, fill)

    def _expand_use(self, element: ET.Element, matrix: Matrix, fill: str | None) -> None:
        href = element.get(XLINK_HREF) or element.get("href") or ""
        target = self._defs.get(href.lstrip("#"))
        if target is None:
            return
        x = float(element.get("x", "0"))
        y = float(element.get("y", "0"))
        placed = _compose(matrix, (1.0, 0.0, 0.0, 1.0, x, y))
        for child in target.iter():
            if _localname(child) == "path":
                self._emit_path(child, placed, fill)

    def _emit_path(
        self, element: ET.Element, ctm: Matrix, inherited_fill: str | None
    ) -> None:
        d = element.get("d")
        if not d:
            return
        matrix = _compose(ctm, _parse_transform(element.get("transform")))
        fill = element.get("fill", inherited_fill or "black")
        stroke = element.get("stroke", "none")
        filled = fill not in (None, "none")
        stroked = stroke not in (None, "none")
        if not filled and not stroked:
            return

        stroke_width_pt: float | None = None
        if stroked:
            raw_width = element.get("stroke-width", "1")
            stroke_width_pt = float(raw_width) * _scale_factor(matrix)
        color = _parse_color(stroke if stroked else fill)

        for points, closed in parse_path_data(d):
            mapped = [_apply(matrix, x, y) for x, y in points]
            if filled and not stroked and _is_page_background(
                mapped, self._geometry.width_pt, self._geometry.height_pt
            ):
                continue
            self._geometry.shapes.append(
                _Shape(
                    points=mapped,
                    closed=closed,
                    filled=filled and not stroked,
                    stroke_width_pt=stroke_width_pt,
                    color=color,
                )
            )


def _extract_geometry(svg_text: str) -> _SvgGeometry:
    try:
        root = _safe_fromstring(svg_text)
    except (ET.ParseError, DefusedXmlException) as err:
        raise KicadConversionError(f"Invalid SVG document: {err}") from err

    width, height = _page_dimensions(root)
    geometry = _SvgGeometry(width_pt=width, height_pt=height)
    walker = _GeometryWalker(_collect_defs(root), geometry)
    for child in root:
        walker.walk(child, IDENTITY, None)
    return geometry


def stroked_line_geometry(
    svg_text: str,
) -> tuple[tuple[float, float], list[list[tuple[float, float]]]]:
    """Page size and stroked (wire/outline) polylines of a schematic SVG.

    Returns ``((width_pt, height_pt), polylines)`` in page pt. Filled shapes —
    text glyph outlines, junction dots, arrows — are excluded; what remains is
    the line work used for connectivity tracing (see :mod:`net_trace`).
    """
    geometry = _extract_geometry(svg_text)
    polylines = [
        shape.points
        for shape in geometry.shapes
        if shape.stroke_width_pt is not None and len(shape.points) >= 2
    ]
    return ((geometry.width_pt, geometry.height_pt), polylines)


# --------------------------------------------------------------------------- #
# KiCad serialization                                                          #
# --------------------------------------------------------------------------- #


def _fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def _shape_to_sexpr(shape: _Shape) -> str | None:
    points = [(x * PT_TO_MM, y * PT_TO_MM) for x, y in shape.points]
    # Drop consecutive duplicates introduced by curve flattening.
    deduped: list[tuple[float, float]] = []
    for point in points:
        if not deduped or math.dist(deduped[-1], point) > 1e-4:
            deduped.append(point)
    if shape.closed and deduped and math.dist(deduped[0], deduped[-1]) > 1e-4:
        deduped.append(deduped[0])
    if len(deduped) < 2:
        return None

    if shape.stroke_width_pt is not None:
        width_mm = max(0.01, shape.stroke_width_pt * PT_TO_MM)
    elif shape.filled:
        width_mm = 0.01
    else:
        width_mm = DEFAULT_STROKE_WIDTH_MM

    xy = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in deduped)
    stroke = f"(stroke (width {_fmt(width_mm)}) (type solid)"
    if shape.color is not None:
        r, g, b = shape.color
        stroke += f" (color {r} {g} {b} 1)"
    stroke += ")"
    fill = "(fill (type outline))" if shape.filled and shape.closed else "(fill (type none))"
    return (
        f'  (polyline (pts {xy}) {stroke} {fill} (uuid "{uuid.uuid4()}"))'
    )


def svg_to_kicad_sch(svg_text: str, *, title: str | None = None) -> str:
    """Convert a pdftocairo schematic SVG into a KiCad schematic document.

    Raises :class:`KicadConversionError` when the SVG cannot be parsed.
    """
    geometry = _extract_geometry(svg_text)

    lines: list[str] = []
    lines.append("(kicad_sch")
    lines.append(f"  (version {KICAD_SCH_VERSION})")
    lines.append('  (generator "docling_serve_schematic_extractor")')
    lines.append('  (generator_version "1.0")')
    lines.append(f'  (uuid "{uuid.uuid4()}")')
    lines.append(
        f'  (paper "User" {_fmt(geometry.width_pt * PT_TO_MM)} '
        f"{_fmt(geometry.height_pt * PT_TO_MM)})"
    )
    if title:
        escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  (title_block (title "{escaped}"))')
    lines.append("  (lib_symbols)")
    for shape in geometry.shapes:
        sexpr = _shape_to_sexpr(shape)
        if sexpr is not None:
            lines.append(sexpr)
    lines.append('  (sheet_instances (path "/" (page "1")))')
    lines.append(")")
    return "\n".join(lines) + "\n"
