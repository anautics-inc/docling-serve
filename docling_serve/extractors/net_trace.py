"""Deterministic wire-connectivity tracing for schematic pages.

The vision model is good at *identifying* components (refDes, type, bounding
box) but unreliable at *tracing* dozens of wires across a dense page. This
module does the tracing geometrically instead: given the page's stroked vector
geometry (from the pdftocairo SVG, see :mod:`kicad_sch`) and the component
bounding boxes located by the model, it

1. explodes stroked polylines into atomic segments,
2. removes the parts inside component boxes (symbol artwork), remembering
   which boxes each surviving wire piece touches — those are pin attachments,
3. unions wire pieces that meet **end-to-line** (straight joins and
   T-junctions connect; X-crossings of two wire interiors do *not*, matching
   schematic semantics where a crossing without a junction is not a contact),
4. reports every cluster touching ≥ 2 components as a net.

Connectivity therefore comes from the drawing itself; the model only names
things. Pure Python, no external geometry dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise

#: Wire pieces whose endpoint is within this distance (pt) of another wire
#: count as connected; also the snap distance from a wire end to a component
#: box edge. Drawings place joints exactly; this only absorbs float noise.
TOUCH_TOLERANCE_PT = 0.35

#: A clipped wire piece shorter than this (pt) is discarded as a sliver.
MIN_WIRE_PIECE_PT = 0.5

#: Wire endpoints within this distance (pt) of a component box edge are
#: treated as attached to that component (pin escape stubs).
PIN_SNAP_PT = 1.0

#: A truly dangling wire end (touching no other wire) may still be a pin
#: connection drawn short of the model's slightly-off bounding box — attach it
#: to the nearest box within this radius...
FREE_END_SNAP_PT = 20.0

#: ...but only when that box is the UNAMBIGUOUS candidate: the second-nearest
#: box must be at least this factor farther away. In dense symbol rows the
#: ratio fails and the end stays unattached rather than guessing.
FREE_END_AMBIGUITY_RATIO = 2.0

#: Spatial-hash cell size (pt) for the connectivity query grid.
GRID_CELL_PT = 8.0

Pt = tuple[float, float]


@dataclass(slots=True)
class ComponentBox:
    """Axis-aligned bounding box of one component symbol on the page (pt)."""

    ref: str
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x1 < self.x0:
            self.x0, self.x1 = self.x1, self.x0
        if self.y1 < self.y0:
            self.y0, self.y1 = self.y1, self.y0

    def contains(self, p: Pt, *, pad: float = 0.0) -> bool:
        return (
            self.x0 - pad <= p[0] <= self.x1 + pad
            and self.y0 - pad <= p[1] <= self.y1 + pad
        )

    def distance(self, p: Pt) -> float:
        """Distance from ``p`` to the box (0 when inside)."""
        dx = max(self.x0 - p[0], 0.0, p[0] - self.x1)
        dy = max(self.y0 - p[1], 0.0, p[1] - self.y1)
        return math.hypot(dx, dy)

    def clip_interval(self, a: Pt, b: Pt) -> tuple[float, float] | None:
        """Parameter range of segment ``a→b`` inside the box (slab method)."""
        t0, t1 = 0.0, 1.0
        for axis in (0, 1):
            lo = (self.x0, self.y0)[axis]
            hi = (self.x1, self.y1)[axis]
            origin = a[axis]
            delta = b[axis] - a[axis]
            if abs(delta) < 1e-12:
                if origin < lo or origin > hi:
                    return None
                continue
            ta = (lo - origin) / delta
            tb = (hi - origin) / delta
            if ta > tb:
                ta, tb = tb, ta
            t0 = max(t0, ta)
            t1 = min(t1, tb)
            if t0 > t1:
                return None
        return (t0, t1)


@dataclass(slots=True)
class TracedNet:
    """One geometric net: the component refs it connects and its wire pieces."""

    components: list[str]
    segments: list[tuple[Pt, Pt]] = field(default_factory=list)
    #: Physical connection points per component ref — where the net's wires
    #: meet that component's bounding box (page pt). One entry per distinct
    #: attachment, so a component wired to this net twice shows two points.
    attachments: dict[str, list[Pt]] = field(default_factory=dict)


@dataclass(slots=True)
class _WirePiece:
    a: Pt
    b: Pt
    touched: set[int]  # indices into the component box list


def _point_segment_distance(p: Pt, a: Pt, b: Pt) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-18:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _lerp(a: Pt, b: Pt, t: float) -> Pt:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _clip_segment(
    a: Pt, b: Pt, boxes: list[ComponentBox]
) -> list[tuple[Pt, Pt, set[int]]]:
    """Split ``a→b`` into the pieces outside every box.

    Returns ``(start, end, touched_box_indices)`` pieces, where ``touched``
    holds boxes the piece was clipped against (i.e. the wire enters that
    component there).
    """
    # Collect the parameter intervals covered by boxes.
    covered: list[tuple[float, float, int]] = []
    for index, box in enumerate(boxes):
        interval = box.clip_interval(a, b)
        if interval is not None and interval[1] - interval[0] > 1e-12:
            covered.append((interval[0], interval[1], index))
    if not covered:
        return [(a, b, set())]

    covered.sort()
    pieces: list[tuple[Pt, Pt, set[int]]] = []
    cursor = 0.0
    open_boxes: list[tuple[float, int]] = []  # (end, box index) currently covering
    events = covered
    for start, end, box_index in events:
        if start > cursor:
            piece_touch = {box_index}
            piece_touch.update(bi for e, bi in open_boxes if e >= cursor)
            pieces.append((_lerp(a, b, cursor), _lerp(a, b, start), piece_touch))
        open_boxes.append((end, box_index))
        cursor = max(cursor, end)
    if cursor < 1.0:
        piece_touch = {bi for e, bi in open_boxes if e >= cursor}
        pieces.append((_lerp(a, b, cursor), _lerp(a, b, 1.0), piece_touch))
    return [
        (start, end, touch)
        for start, end, touch in pieces
        if math.dist(start, end) >= MIN_WIRE_PIECE_PT
    ]


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        parent = self._parent
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


class _SegmentGrid:
    """Spatial hash over wire pieces for neighbour queries."""

    def __init__(self, pieces: list[_WirePiece]) -> None:
        self._cells: dict[tuple[int, int], list[int]] = {}
        for index, piece in enumerate(pieces):
            for cell in self._cells_for(piece.a, piece.b, pad=TOUCH_TOLERANCE_PT):
                self._cells.setdefault(cell, []).append(index)

    @staticmethod
    def _cells_for(a: Pt, b: Pt, *, pad: float):
        x0 = int((min(a[0], b[0]) - pad) // GRID_CELL_PT)
        x1 = int((max(a[0], b[0]) + pad) // GRID_CELL_PT)
        y0 = int((min(a[1], b[1]) - pad) // GRID_CELL_PT)
        y1 = int((max(a[1], b[1]) + pad) // GRID_CELL_PT)
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                yield (cx, cy)

    def candidates(self, piece: _WirePiece) -> set[int]:
        found: set[int] = set()
        for cell in self._cells_for(piece.a, piece.b, pad=TOUCH_TOLERANCE_PT):
            found.update(self._cells.get(cell, ()))
        return found


def _touches(p1: _WirePiece, p2: _WirePiece) -> bool:
    """End-to-line contact (joins and T-junctions), never interior X-crossing."""
    return (
        _point_segment_distance(p1.a, p2.a, p2.b) < TOUCH_TOLERANCE_PT
        or _point_segment_distance(p1.b, p2.a, p2.b) < TOUCH_TOLERANCE_PT
        or _point_segment_distance(p2.a, p1.a, p1.b) < TOUCH_TOLERANCE_PT
        or _point_segment_distance(p2.b, p1.a, p1.b) < TOUCH_TOLERANCE_PT
    )


def _wire_pieces(
    polylines: list[list[Pt]], boxes: list[ComponentBox]
) -> list[_WirePiece]:
    """Clip page line work to outside-of-box wire pieces with pin attachments."""
    pieces: list[_WirePiece] = []
    for polyline in polylines:
        for a, b in pairwise(polyline):
            if math.dist(a, b) < 1e-9:
                continue
            for start, end, touched in _clip_segment(a, b, boxes):
                pieces.append(_WirePiece(a=start, b=end, touched=set(touched)))

    # Snap free wire ends that stop just short of a component box.
    for piece in pieces:
        for p in (piece.a, piece.b):
            for index, box in enumerate(boxes):
                if index not in piece.touched and box.contains(p, pad=PIN_SNAP_PT):
                    piece.touched.add(index)
    return pieces


def _rescue_free_ends(
    pieces: list[_WirePiece], boxes: list[ComponentBox], grid: _SegmentGrid
) -> None:
    """Attach dangling wire ends to the nearest nearby component box.

    Vision-model bounding boxes are routinely a few points off the drawn
    symbol, so a wire can stop just short of the box it clearly connects to.
    Only truly free ends (touching no other wire piece) are snapped, and only
    within :data:`FREE_END_SNAP_PT` — a mid-net junction can never bridge into
    an unrelated component this way.
    """
    for index, piece in enumerate(pieces):
        for p in (piece.a, piece.b):
            touching_wire = any(
                other != index
                and _point_segment_distance(p, pieces[other].a, pieces[other].b)
                < TOUCH_TOLERANCE_PT
                for other in grid.candidates(piece)
            )
            if touching_wire:
                continue
            ranked = sorted(
                (box.distance(p), box_index) for box_index, box in enumerate(boxes)
            )
            if not ranked or ranked[0][0] > FREE_END_SNAP_PT:
                continue
            unambiguous = (
                len(ranked) < 2
                or ranked[1][0] >= ranked[0][0] * FREE_END_AMBIGUITY_RATIO
            )
            if unambiguous:
                piece.touched.add(ranked[0][1])


def trace_nets(
    polylines: list[list[Pt]],
    boxes: list[ComponentBox],
) -> list[TracedNet]:
    """Trace electrical nets from stroked page geometry and component boxes.

    ``polylines`` are the page's stroked vector paths in page coordinates
    (pt). Returns nets sorted by descending component count; components
    within each net are sorted by ref.
    """
    pieces = _wire_pieces(polylines, boxes)
    grid = _SegmentGrid(pieces)
    _rescue_free_ends(pieces, boxes, grid)
    uf = _UnionFind(len(pieces))
    for index, piece in enumerate(pieces):
        for other in grid.candidates(piece):
            if other > index and _touches(piece, pieces[other]):
                uf.union(index, other)

    clusters: dict[int, list[int]] = {}
    for index in range(len(pieces)):
        clusters.setdefault(uf.find(index), []).append(index)

    nets: list[TracedNet] = []
    for members in clusters.values():
        touched: set[int] = set()
        for index in members:
            touched.update(pieces[index].touched)
        if len(touched) < 2:
            continue
        nets.append(
            TracedNet(
                components=sorted(boxes[i].ref for i in touched),
                segments=[(pieces[i].a, pieces[i].b) for i in members],
                attachments=_attachment_points(members, pieces, boxes),
            )
        )
    nets.sort(key=lambda net: (-len(net.components), net.components))
    return nets


#: Attachment points closer than this (pt) collapse into one physical pin.
_ATTACHMENT_MERGE_PT = 3.0


def _attachment_points(
    members: list[int], pieces: list[_WirePiece], boxes: list[ComponentBox]
) -> dict[str, list[Pt]]:
    """Where this net's wires physically meet each component's box.

    For every wire piece that touches a box, the piece endpoint nearest the
    box is the connection point (clipping placed it on/near the box edge).
    Points within :data:`_ATTACHMENT_MERGE_PT` of each other are one pin.
    """
    by_ref: dict[str, list[Pt]] = {}
    for index in members:
        piece = pieces[index]
        for box_index in piece.touched:
            box = boxes[box_index]
            point = min((piece.a, piece.b), key=box.distance)
            bucket = by_ref.setdefault(box.ref, [])
            if all(math.dist(point, existing) >= _ATTACHMENT_MERGE_PT for existing in bucket):
                bucket.append(point)
    return by_ref
