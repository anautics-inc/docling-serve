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
from typing import Any

from defusedxml.common import DefusedXmlException

# Parse SVG with defusedxml to block entity-expansion / external-entity attacks.
# ``ET`` is retained for Element type hints and ``ParseError``.
from defusedxml.ElementTree import fromstring as _safe_fromstring

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

#: SVG user units from pdftocairo are PDF points (1/72 inch).
PT_TO_MM = 25.4 / 72.0

#: KiCad 8.0 schematic file format version.
#: Schema version token matching what KiCad 10 (eeschema 10.0) writes when it
#: saves — taken from a real eeschema-rewritten file so generated documents
#: are first-class current-format citizens, not "older version" imports.
KICAD_SCH_VERSION = 20260306

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
_PATH_COMMAND_RE = re.compile(
    r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)"
)


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
    net_length = math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
    segments = max(
        2,
        min(MAX_CURVE_SEGMENTS, math.ceil(math.sqrt(net_length / CURVE_TOLERANCE_PT))),
    )
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
                op,
                read(_CURVE_ARITY[op]),
                pos,
                (ox, oy),
                prev_cubic_ctrl,
                prev_quad_ctrl,
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
    return (max(xs) - min(xs)) >= 0.95 * width and (max(ys) - min(ys)) >= 0.95 * height


#: Longest dimension (pt) a filled path may have and still replay as a filled
#: KiCad polygon. KiCad's polyline fill paints with the STROKE color ("outline"
#: fill), which is only correct for glyph outlines, junction dots, arrowheads
#: and similar solid marks — all glyph-scale. Larger filled paths (component
#: body tints, title-block panels, border rings drawn as even-odd hole
#: subpaths) would flood their area with an opaque rectangle and bury the
#: line work under it, so they are demoted to their outline.
_MAX_FILLED_SHAPE_PT = 20.0


def _is_marker_sized(points: list[tuple[float, float]]) -> bool:
    """True when a shape is small enough to be a glyph or solid marker."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (
        max(xs) - min(xs) <= _MAX_FILLED_SHAPE_PT
        and max(ys) - min(ys) <= _MAX_FILLED_SHAPE_PT
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

    _SKIPPED_TAGS = frozenset(
        {"defs", "clipPath", "mask", "symbol", "metadata", "style"}
    )

    def __init__(
        self, defs_table: dict[str, ET.Element], geometry: _SvgGeometry
    ) -> None:
        self._defs = defs_table
        self._geometry = geometry

    def walk(
        self, element: ET.Element, ctm: Matrix, inherited_fill: str | None
    ) -> None:
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

    def _expand_use(
        self, element: ET.Element, matrix: Matrix, fill: str | None
    ) -> None:
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
            fill_only = filled and not stroked
            if fill_only and _is_page_background(
                mapped, self._geometry.width_pt, self._geometry.height_pt
            ):
                continue
            self._geometry.shapes.append(
                _Shape(
                    points=mapped,
                    closed=closed,
                    filled=fill_only and _is_marker_sized(mapped),
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
    fill = (
        "(fill (type outline))"
        if shape.filled and shape.closed
        else "(fill (type none))"
    )
    return f'  (polyline (pts {xy}) {stroke} {fill} (uuid "{uuid.uuid4()}"))'


#: KiCad renders embedded bitmaps at 300 DPI when scale is 1.0.
KICAD_IMAGE_BASE_DPI = 300.0

#: Endpoints closer than this (pt) count as one junction point.
_JUNCTION_TOLERANCE_PT = 1.5


def net_display_name(net: dict[str, Any]) -> str | None:
    """The label KiCad should carry for this net: printed name, else wire id.

    Every net has a wire id after connectivity-id assignment, so every net's
    copper can carry its identity — the label is what electrically JOINS the
    net's islands (traced wire runs, symbol stub wires) into ONE net without
    fabricating bridging geometry.

    Ground nets are all labeled ``GND``: ngspice aliases that name to node 0,
    so KiCad's simulator gets its reference — the same unification the SPICE
    emitter performs by collapsing ground nets to ``0``.
    """
    from docling_serve.schematic.spice import is_ground_net

    if is_ground_net(net):
        return "GND"
    name = str(net.get("name") or "").strip()
    if name:
        return name
    wire_id = str(net.get("wireId") or "").strip()
    return wire_id or None


def _label_sexpr(name: str, x_mm: float, y_mm: float) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'  (label "{escaped}" (at {_fmt(x_mm)} {_fmt(y_mm)} 0) '
        f"(effects (font (size 1.27 1.27)) (justify left bottom)) "
        f'(uuid "{uuid.uuid4()}"))'
    )


def _global_label_sexpr(name: str, x_mm: float, y_mm: float) -> str:
    """A global label — the schematic-standard terminator for an off-page or
    island endpoint. Connectivity-wise it counts as a real connection (a pin
    wired only to it is CONNECTED for ERC), and same-name labels join their
    islands into one electrical net across the whole document."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'  (global_label "{escaped}" (shape passive) (at {_fmt(x_mm)} {_fmt(y_mm)} 0) '
        f"(effects (font (size 1.27 1.27)) (justify left)) "
        f'(uuid "{uuid.uuid4()}"))'
    )


def net_label_sexprs(nets: list[dict[str, Any]], *, page_no: int) -> list[str]:
    """KiCad ``(label …)`` items naming each net ON its wire.

    A label bound to the copper is how KiCad carries net identity through
    edits — rename, drag, extend, and the netlist still says A8B22. One
    label sits at the midpoint of the net's longest segment; every DANGLING
    segment endpoint (degree 1 within the net — an off-page stub or an
    island the tracer couldn't bridge) gets one too, so all of a net's
    islands join into one electrical net by name.
    """
    items: list[str] = []
    for net in nets:
        if not isinstance(net, dict):
            continue
        name = net_display_name(net)
        if not name:
            continue
        if net.get("page") not in (None, page_no):
            continue
        segments = [
            s
            for s in net.get("segments") or []
            if isinstance(s, (list, tuple)) and len(s) == 4
        ]
        if not segments:
            # No traced copper: the net's stub wires are labeled where they
            # are emitted (build_symbol_instances) — a floating label here
            # would only dangle.
            continue
        danglers = _dangling_endpoints(segments)
        if danglers:
            # Dangling copper ends are off-page runs or islands the tracer
            # couldn't bridge — terminate each with a GLOBAL label so ERC
            # sees intent and same-name islands merge electrically. Global
            # everywhere (stub ends are global too): mixing in a local twin
            # would trip ERC's same_local_global_label.
            for px, py in danglers:
                items.append(_global_label_sexpr(name, px * PT_TO_MM, py * PT_TO_MM))
            continue
        # Closed copper (no dangling ends): one on-wire identity label,
        # nudged off junction points (a label ON a junction trips ERC's
        # label_multiple_wires).
        longest = max(segments, key=lambda s: (s[2] - s[0]) ** 2 + (s[3] - s[1]) ** 2)
        junction_keys = {
            key
            for key, points in _endpoint_buckets(segments).items()
            if len(points) >= 3
        }
        for fraction in (0.5, 0.25, 0.75):
            mx_pt = (
                float(longest[0]) + (float(longest[2]) - float(longest[0])) * fraction
            )
            my_pt = (
                float(longest[1]) + (float(longest[3]) - float(longest[1])) * fraction
            )
            key = (
                round(mx_pt / _JUNCTION_TOLERANCE_PT),
                round(my_pt / _JUNCTION_TOLERANCE_PT),
            )
            if key not in junction_keys:
                break
        items.append(_global_label_sexpr(name, mx_pt * PT_TO_MM, my_pt * PT_TO_MM))
    return items


def _endpoint_buckets(
    segments: list[Any],
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Segment endpoints grouped by junction-tolerance bucket, in pt."""
    counts: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for segment in segments:
        for px, py in ((segment[0], segment[1]), (segment[2], segment[3])):
            key = (
                round(float(px) / _JUNCTION_TOLERANCE_PT),
                round(float(py) / _JUNCTION_TOLERANCE_PT),
            )
            counts.setdefault(key, []).append((float(px), float(py)))
    return counts


def _dangling_endpoints(
    segments: list[Any],
) -> list[tuple[float, float]]:
    """Endpoints touched by exactly one segment (within one net), in pt."""
    return [
        points[0] for points in _endpoint_buckets(segments).values() if len(points) == 1
    ]


def junction_sexprs(nets: list[dict[str, Any]], *, page_no: int) -> list[str]:
    """KiCad ``(junction …)`` dots where three or more wire ends meet.

    Junction dots are how a schematic asserts T-connections; without them
    KiCad renders bare crossings and edits can silently split nets.
    """
    items: list[str] = []
    for net in nets:
        if not isinstance(net, dict):
            continue
        if net.get("page") not in (None, page_no):
            continue
        counts: dict[tuple[float, float], int] = {}
        for segment in net.get("segments") or []:
            if not (isinstance(segment, (list, tuple)) and len(segment) == 4):
                continue
            for px, py in ((segment[0], segment[1]), (segment[2], segment[3])):
                key = (
                    round(float(px) / _JUNCTION_TOLERANCE_PT),
                    round(float(py) / _JUNCTION_TOLERANCE_PT),
                )
                counts[key] = counts.get(key, 0) + 1
        for (kx, ky), count in counts.items():
            if count < 3:
                continue
            mx = kx * _JUNCTION_TOLERANCE_PT * PT_TO_MM
            my = ky * _JUNCTION_TOLERANCE_PT * PT_TO_MM
            items.append(
                f"  (junction (at {_fmt(mx)} {_fmt(my)}) (diameter 0) "
                f'(color 0 0 0 0) (uuid "{uuid.uuid4()}"))'
            )
    return items


def component_annotation_sexprs(
    components: list[dict[str, Any]], *, page_no: int
) -> list[str]:
    """Component outlines + designator text as KiCad graphic items.

    Extracted components aren't yet symbol instances (that needs symbol
    libraries), but their boxes and printed identities belong in the
    editable document so an engineer editing in KiCad sees WHAT each region
    is and can replace it with a real symbol.
    """
    items: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        if component.get("page") not in (None, page_no):
            continue
        bbox = component.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        x0, y0, x1, y1 = (float(v) * PT_TO_MM for v in bbox)
        items.append(
            f"  (rectangle (start {_fmt(x0)} {_fmt(y0)}) (end {_fmt(x1)} {_fmt(y1)}) "
            f"(stroke (width 0.127) (type dash) (color 132 0 132 1)) "
            f'(fill (type none)) (uuid "{uuid.uuid4()}"))'
        )
        caption = " ".join(
            str(part)
            for part in (component.get("refDes"), component.get("partNumber"))
            if part
        ) or str(component.get("description") or component.get("type") or "")
        if caption:
            caption = caption.replace("\\", "\\\\").replace('"', '\\"')
            items.append(
                f'  (text "{caption}" (at {_fmt(x0)} {_fmt(max(0.0, y0 - 1.0))} 0) '
                f"(effects (font (size 1.27 1.27)) (justify left bottom)) "
                f'(uuid "{uuid.uuid4()}"))'
            )
    return items


#: Vector exports with fewer polylines than this are considered empty (a
#: scanned drawing's only "geometry" is the page frame) and get the raster
#: page embedded so KiCad still opens the drawing as a tracing backdrop.
MIN_VECTOR_SHAPES = 5


def raster_image_sexpr(
    png_bytes: bytes, *, dpi: float, width_px: int, height_px: int
) -> str:
    """A standard KiCad ``(image ...)`` token holding one page render.

    Positioned so the bitmap covers the page from the origin at true physical
    size (KiCad assumes 300 DPI at scale 1.0, so scale corrects the render
    DPI).
    """
    import base64

    scale = KICAD_IMAGE_BASE_DPI / dpi
    center_x_mm = width_px / dpi * 25.4 / 2
    center_y_mm = height_px / dpi * 25.4 / 2
    b64 = base64.b64encode(png_bytes).decode("ascii")
    chunks = "\n      ".join(f'"{b64[i : i + 76]}"' for i in range(0, len(b64), 76))
    return (
        f"  (image (at {_fmt(center_x_mm)} {_fmt(center_y_mm)}) "
        f"(scale {_fmt(scale)})\n"
        f'    (uuid "{uuid.uuid4()}")\n'
        f"    (data\n      {chunks}\n    )\n"
        f"  )"
    )


def net_wires_sexpr(nets: list[Any], *, page_no: int) -> list[str]:
    """Real KiCad ``(wire ...)`` objects from a page's traced net segments.

    Geometry replay alone produces graphics (``polyline``) and bitmaps —
    KiCad treats those as decoration, so a drawing FULL of wires opened with
    zero electrical wire objects. Each traced segment becomes an actual wire
    in the exact shape eeschema 10 saves: selectable, draggable, and counted
    by netlist tooling. Segments are page-pt (y-down); KiCad wants mm in the
    same orientation. Duplicates collapse (drawings stroke spans twice).
    """
    seen: set[tuple[float, float, float, float]] = set()
    items: list[str] = []
    for net in nets:
        if not isinstance(net, dict):
            continue
        if net.get("page") not in (None, page_no):
            continue
        for segment in net.get("segments") or []:
            if not (isinstance(segment, (list, tuple)) and len(segment) == 4):
                continue
            key = (
                round(float(segment[0]), 2),
                round(float(segment[1]), 2),
                round(float(segment[2]), 2),
                round(float(segment[3]), 2),
            )
            if key in seen:
                continue
            seen.add(key)
            x1, y1, x2, y2 = (float(v) * PT_TO_MM for v in segment)
            items.append(
                f"  (wire (pts (xy {_fmt(x1)} {_fmt(y1)}) (xy {_fmt(x2)} {_fmt(y2)})) "
                f'(stroke (width 0) (type default)) (uuid "{uuid.uuid4()}"))'
            )
    return items


#: ERC severities for GENERATED replay documents, shipped as a sibling
#: ``.kicad_pro``. Each entry is STRUCTURAL for reverse-engineered sheets,
#: not a hidden defect (the graph-level delivery checks report extraction
#: gaps honestly):
#: * endpoint_off_grid — geometry replays the page's own coordinates; the
#:   drawing is the ground truth, not KiCad's edit grid.
#: * isolated_pin_label — off-page runs legitimately reach one pin on this
#:   sheet and terminate in a named global label.
#: * pin_to_pin / power_pin_not_driven — stand-in symbols (generic blocks,
#:   GND glyphs) carry approximate pin TYPES; no power source symbols exist
#:   by design, the SPICE deck injects the supplies.
_PROJECT_ERC_SEVERITIES = {
    "endpoint_off_grid": "ignore",
    "isolated_pin_label": "ignore",
    "pin_to_pin": "ignore",
    "power_pin_not_driven": "ignore",
}


def project_file_json() -> str:
    """A minimal ``.kicad_pro`` carrying the generated-document ERC policy."""
    import json as _json

    return _json.dumps(
        {
            "board": {},
            "erc": {"rule_severities": dict(_PROJECT_ERC_SEVERITIES)},
            "meta": {"filename": "generated.kicad_pro", "version": 3},
            "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        },
        indent=2,
    )


def write_project_files(kicad_sch_paths: list[Any]) -> list[Any]:
    """Sibling ``<stem>.kicad_pro`` for every generated schematic document."""
    written = []
    for sch_path in kicad_sch_paths:
        pro_path = sch_path.with_suffix(".kicad_pro")
        pro_path.write_text(project_file_json())
        written.append(pro_path)
    return written


def inject_items(kicad_text: str, items: list[str]) -> str:
    """Insert top-level items into a ``.kicad_sch`` document body."""
    if not items:
        return kicad_text
    body = "\n".join(items)
    marker = "  (sheet_instances"
    if marker in kicad_text:
        return kicad_text.replace(marker, body + "\n" + marker, 1)
    return kicad_text.rstrip()[:-1] + body + "\n)\n"


def embed_raster_page(
    kicad_text: str, png_bytes: bytes, *, dpi: float, width_px: int, height_px: int
) -> str:
    """Insert a page-render image into an existing ``.kicad_sch`` document."""
    image = raster_image_sexpr(
        png_bytes, dpi=dpi, width_px=width_px, height_px=height_px
    )
    return inject_items(kicad_text, [image])


def raster_page_to_kicad_sch(
    png_bytes: bytes,
    *,
    dpi: float,
    width_px: int,
    height_px: int,
    title: str | None = None,
) -> str:
    """A ``.kicad_sch`` whose only content is the embedded page render.

    Used when a drawing has no vector geometry at all (scanned source) so the
    KiCad artifact still exists and opens to the actual drawing.
    """
    width_mm = width_px / dpi * 25.4
    height_mm = height_px / dpi * 25.4
    lines: list[str] = []
    lines.append("(kicad_sch")
    lines.append(f"  (version {KICAD_SCH_VERSION})")
    lines.append('  (generator "docling_serve_schematic_extractor")')
    lines.append('  (generator_version "1.0")')
    lines.append(f'  (uuid "{uuid.uuid4()}")')
    lines.append(f'  (paper "User" {_fmt(width_mm)} {_fmt(height_mm)})')
    if title:
        escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  (title_block (title "{escaped}"))')
    lines.append("  (lib_symbols)")
    lines.append(
        raster_image_sexpr(png_bytes, dpi=dpi, width_px=width_px, height_px=height_px)
    )
    lines.append('  (sheet_instances (path "/" (page "1")))')
    lines.append(")")
    return "\n".join(lines) + "\n"


#: Root-page layout for hierarchy sheets (mm): box size and grid spacing.
_SHEET_BOX_W_MM = 70.0
_SHEET_BOX_H_MM = 30.0
_SHEET_GAP_MM = 12.0
_SHEET_MARGIN_MM = 20.0
_SHEETS_PER_ROW = 3


def hierarchy_root_sexpr(page_files: list[str], *, title: str | None = None) -> str:
    """A root ``.kicad_sch`` linking each page file as a hierarchical sheet.

    Multi-page drawings export one KiCad document per page; without a root,
    KiCad sees disconnected files and cross-page nets never join in its
    netlister. The root lays a sheet symbol per page (grid layout on an A3
    sheet), so opening it loads the WHOLE drawing as one hierarchy and
    "next sheet" navigation works as engineers expect.
    """
    root_uuid = uuid.uuid4()
    lines: list[str] = []
    lines.append("(kicad_sch")
    lines.append(f"  (version {KICAD_SCH_VERSION})")
    lines.append('  (generator "docling_serve_schematic_extractor")')
    lines.append('  (generator_version "1.0")')
    lines.append(f'  (uuid "{root_uuid}")')
    lines.append('  (paper "A3")')
    if title:
        escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  (title_block (title "{escaped}"))')
    lines.append("  (lib_symbols)")
    for index, page_file in enumerate(page_files):
        column = index % _SHEETS_PER_ROW
        row = index // _SHEETS_PER_ROW
        x = _SHEET_MARGIN_MM + column * (_SHEET_BOX_W_MM + _SHEET_GAP_MM)
        y = _SHEET_MARGIN_MM + row * (_SHEET_BOX_H_MM + _SHEET_GAP_MM)
        sheet_uuid = uuid.uuid4()
        name = f"Page {index + 1}"
        lines.append(
            f"  (sheet (at {_fmt(x)} {_fmt(y)}) "
            f"(size {_fmt(_SHEET_BOX_W_MM)} {_fmt(_SHEET_BOX_H_MM)})\n"
            "    (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0))\n"
            f'    (uuid "{sheet_uuid}")\n'
            f'    (property "Sheetname" "{name}" (at {_fmt(x)} {_fmt(y - 0.8)} 0)\n'
            "      (effects (font (size 1.27 1.27)) (justify left bottom)))\n"
            f'    (property "Sheetfile" "{page_file}" '
            f"(at {_fmt(x)} {_fmt(y + _SHEET_BOX_H_MM + 0.8)} 0)\n"
            "      (effects (font (size 1.27 1.27)) (justify left top)))\n"
            '    (instances (project ""\n'
            f'      (path "/{root_uuid}" (page "{index + 2}"))))\n'
            "  )"
        )
    lines.append('  (sheet_instances (path "/" (page "1")))')
    lines.append(")")
    return "\n".join(lines) + "\n"


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
