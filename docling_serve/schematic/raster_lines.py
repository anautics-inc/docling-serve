"""Wire-line extraction from SCANNED drawing pages (classical CV, no model).

Scanned schematics have no vector geometry, so the geometric net tracer —
the thing that makes wires clickable and connectivity factual — used to get
nothing. This module synthesizes the tracer's input from the raster via
skeleton-based centerline tracing (the standard technique for engineering
drawing vectorization; outline tracers like VTracer/potrace produce contour
polygons, which is the wrong shape for line work):

1. Binarize the page (Otsu) and drop SATURATED pixels (colored annotation
   boxes drawn over archive scans are not circuitry).
2. Mask the PDF text layer (wire labels sit ON the wires; without masking
   every label glues its wire to the next one).
3. Remove LARGE ink fills (symbol solids, title-block bars) but keep small
   ones — junction dots are connectivity, not noise.
4. Skeletonize and walk the skeleton graph into centerline chains; prune
   dangling spurs (symbol remnants).
5. Resolve crossings: at a 4-way node WITHOUT a junction dot, collinear
   chain pairs merge into through-going polylines so crossing wires do NOT
   connect (drafting semantics); a dot keeps all four arms joined.
6. Bridge chain gaps — dashes, dot holes, and masked-label holes — by
   joining near-collinear endpoints (larger reach inside text boxes).

The output is polylines in page-pt space — exactly what
:func:`net_trace.trace_nets` consumes for vector drawings, so everything
downstream (junction detection, component clipping, attachment points,
clickable wire overlays) works identically on scans.
"""

from __future__ import annotations

import logging
from math import atan2, cos, degrees, hypot, sin
from typing import Any

_log = logging.getLogger(__name__)

Pt = tuple[float, float]
Box = tuple[float, float, float, float]

#: HSV saturation above this marks colored (annotation) ink to drop.
ANNOTATION_SATURATION = 80

#: Padding (px) around masked text-layer rectangles.
TEXT_MASK_PAD_PX = 2

#: Ink that survives a (2*r+1)² erosion is a fill region; fills LARGER than
#: this area (px²) are removed (symbol solids), smaller ones kept (junction
#: dots, arrowheads — tiny spurs that pruning handles).
FILL_ERODE_RADIUS_PX = 2
MAX_KEPT_FILL_AREA_PX = 150

#: Dangling skeleton branches shorter than this are symbol remnants.
SPUR_MIN_PX = 12.0

#: Douglas-Peucker tolerance for chain simplification.
SIMPLIFY_EPS_PX = 2.0

#: Polylines shorter than this total run are noise.
MIN_WIRE_RUN_PX = 18.0

#: A straight chain spanning more than this fraction of the page dimension
#: is the drawing border / title-block frame, not a wire.
MAX_WIRE_SPAN_FRAC = 0.65

#: Two chain ends join across a gap when within this distance and roughly
#: collinear — dashes and junction-dot holes. Inside a masked text box the
#: reach grows to the text limit (a label hole must not split its wire).
BRIDGE_GAP_PX = 18.0
BRIDGE_TEXT_GAP_PX = 90.0
BRIDGE_MAX_ANGLE_DEG = 20.0

#: Crossing resolution: two arms at a junction merge into a through-going
#: polyline when their directions are at least this opposed.
THROUGH_MIN_ANGLE_DEG = 150.0

#: A junction whose distance-transform value exceeds the page's typical wire
#: half-thickness by this factor carries a junction DOT (connected crossing).
DOT_THICKNESS_FACTOR = 2.2


def raster_wire_polylines(
    page_png: bytes,
    *,
    page_size_pt: tuple[float, float],
    text_boxes_pt: list[Box] | None = None,
) -> list[list[Pt]]:
    """Extract wire centerline polylines (page-pt space) from a page render.

    Returns ``[]`` when dependencies are unavailable or the page yields
    nothing — callers fall back to the model's own nets exactly as before.
    """
    try:
        import cv2
        import numpy as np
        from skimage.morphology import skeletonize
    except ImportError as error:  # pragma: no cover - ships with OCR stack
        _log.warning("Raster wire extraction unavailable: %s", error)
        return []

    color = cv2.imdecode(np.frombuffer(page_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if color is None:
        return []
    height_px, width_px = color.shape[:2]
    page_w_pt, page_h_pt = page_size_pt
    if page_w_pt <= 0 or page_h_pt <= 0:
        return []
    scale_x, scale_y = width_px / page_w_pt, height_px / page_h_pt

    text_boxes_px = [
        (
            x0 * scale_x - TEXT_MASK_PAD_PX,
            y0 * scale_y - TEXT_MASK_PAD_PX,
            x1 * scale_x + TEXT_MASK_PAD_PX,
            y1 * scale_y + TEXT_MASK_PAD_PX,
        )
        for x0, y0, x1, y1 in text_boxes_pt or []
    ]

    binary = _wire_ink_mask(cv2, np, color, text_boxes_px)
    skeleton = skeletonize(binary > 0)
    distance = cv2.distanceTransform(binary, cv2.DIST_L2, 3)
    chains = _skeleton_chains(cv2, np, skeleton, distance)
    chains = _bridge_chain_gaps(chains, text_boxes_px)

    polylines: list[list[Pt]] = []
    for chain in chains:
        simplified = _simplify(cv2, np, chain)
        if _polyline_length(simplified) < MIN_WIRE_RUN_PX:
            continue
        if _is_page_frame(simplified, width_px, height_px):
            continue
        polylines.append([(x / scale_x, y / scale_y) for x, y in simplified])
    return polylines


# ── Ink mask ────────────────────────────────────────────────────────────────


def _wire_ink_mask(cv2: Any, np: Any, color: Any, text_boxes_px: list[Box]) -> Any:
    """Binary wire ink: no colored annotation, no text, no large fills."""
    gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    saturation = cv2.cvtColor(color, cv2.COLOR_BGR2HSV)[:, :, 1]
    binary[saturation > ANNOTATION_SATURATION] = 0

    for x0, y0, x1, y1 in text_boxes_px:
        cv2.rectangle(binary, (int(x0), int(y0)), (int(x1), int(y1)), 0, -1)

    # Large fills (symbol solids, heavy bars) would skeletonize into wrong
    # medial lines; junction dots and other SMALL fills stay — they are how
    # the drawing says "connected".
    radius = FILL_ERODE_RADIUS_PX
    eroded = cv2.erode(binary, np.ones((2 * radius + 1, 2 * radius + 1), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(eroded, connectivity=8)
    remove = np.zeros_like(binary)
    for index in range(1, count):
        if stats[index][4] >= MAX_KEPT_FILL_AREA_PX:
            remove[labels == index] = 255
    remove = cv2.dilate(remove, np.ones((2 * radius + 3, 2 * radius + 3), np.uint8))
    return cv2.bitwise_and(binary, cv2.bitwise_not(remove))


# ── Skeleton graph walk ─────────────────────────────────────────────────────

_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def _skeleton_chains(cv2: Any, np: Any, skeleton: Any, distance: Any) -> list[list[Pt]]:
    """Centerline chains from the skeleton, with crossings resolved.

    Chains run node-to-node (junctions and endpoints). Dangling spurs are
    pruned. At each 4-way (or higher) junction WITHOUT a junction dot, arms
    whose directions oppose are merged into a single through-going polyline,
    so crossing wires meet only in their interiors — which the net tracer
    correctly treats as NOT connected.
    """
    height, width = skeleton.shape
    kernel = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], np.uint8)
    neighbor_count = cv2.filter2D(skeleton.astype(np.uint8), -1, kernel)
    node_mask = (skeleton & (neighbor_count != 2)).astype(np.uint8)
    _count, node_labels = cv2.connectedComponents(node_mask, connectivity=8)

    skel = skeleton
    visited = np.zeros_like(skel, bool)

    def neighbors(y: int, x: int):
        for dy, dx in _OFFSETS:
            yy, xx = y + dy, x + dx
            if 0 <= yy < height and 0 <= xx < width and skel[yy, xx]:
                yield yy, xx

    def walk(y: int, x: int, yy: int, xx: int) -> dict[str, Any]:
        points = [(float(x), float(y)), (float(xx), float(yy))]
        visited[yy, xx] = True
        cy, cx = yy, xx
        end_label = 0
        while True:
            step = None
            for ny, nx in neighbors(cy, cx):
                if (float(nx), float(ny)) == points[-2]:
                    continue
                if node_mask[ny, nx]:
                    step = (ny, nx, True)
                    break
                if not visited[ny, nx]:
                    step = (ny, nx, False)
                    break
            if step is None:
                break
            ny, nx, at_node = step
            points.append((float(nx), float(ny)))
            if at_node:
                end_label = int(node_labels[ny, nx])
                break
            visited[ny, nx] = True
            cy, cx = ny, nx
        return {"points": points, "start": int(node_labels[y, x]), "end": end_label}

    # Walk degree-2 runs between node pixels.
    raw: list[dict[str, Any]] = []
    node_ys, node_xs = np.where(node_mask > 0)
    for y, x in zip(node_ys, node_xs):
        for yy, xx in neighbors(y, x):
            if not (node_mask[yy, xx] or visited[yy, xx]):
                raw.append(walk(y, x, yy, xx))

    return _resolve_crossings(np, _prune_spurs(raw), distance)


def _prune_spurs(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop short chains with a free end (symbol remnants, ruler ticks)."""
    label_uses: dict[int, int] = {}
    for chain in raw:
        for key in (chain["start"], chain["end"]):
            label_uses[key] = label_uses.get(key, 0) + 1
    return [
        chain
        for chain in raw
        if not (
            _polyline_length(chain["points"]) < SPUR_MIN_PX
            and (
                label_uses.get(chain["start"], 0) <= 1
                or chain["end"] == 0
                or label_uses.get(chain["end"], 0) <= 1
            )
        )
    ]


def _resolve_crossings(
    np: Any, chains: list[dict[str, Any]], distance: Any
) -> list[list[Pt]]:
    """Merge opposing arms at dot-less junctions into through polylines."""
    # Typical wire half-thickness: median distance-transform value over all
    # chain interiors (page-adaptive; no fixed stroke width assumed).
    samples = [
        distance[int(py), int(px)]
        for chain in chains
        for px, py in chain["points"][1:-1][::4]
    ]
    half_thickness = float(np.median(samples)) if samples else 1.0

    by_node: dict[int, list[tuple[int, bool]]] = {}
    for index, chain in enumerate(chains):
        for label, at_start in ((chain["start"], True), (chain["end"], False)):
            if label > 0:
                by_node.setdefault(label, []).append((index, at_start))

    links: list[tuple[tuple[int, bool], tuple[int, bool], Pt]] = []
    for arms in by_node.values():
        if len(arms) < 4:
            continue  # T-junctions and corners connect by touch — correct
        node_point = _arm_point(chains, arms[0], at_node=True)
        if distance[int(node_point[1]), int(node_point[0])] > (
            half_thickness * DOT_THICKNESS_FACTOR
        ):
            continue  # junction dot: every arm is genuinely connected
        links.extend(
            (arm_a, arm_b, node_point)
            for arm_a, arm_b in _pair_opposing_arms(chains, arms)
        )

    # Apply pairwise links with union-find over chain indices.
    parent = list(range(len(chains)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    paths: dict[int, list[Pt]] = {i: list(c["points"]) for i, c in enumerate(chains)}
    for arm_a, arm_b, junction in links:
        root_a, root_b = find(arm_a[0]), find(arm_b[0])
        if root_a == root_b:
            continue
        # Orient by junction proximity (flags go stale after earlier merges):
        # path_a must END at the junction; path_b must START there.
        path_a, path_b = paths[root_a], paths[root_b]
        if _dist(path_a[0], junction) < _dist(path_a[-1], junction):
            path_a = path_a[::-1]
        if _dist(path_b[-1], junction) < _dist(path_b[0], junction):
            path_b = path_b[::-1]
        paths[root_a] = path_a + path_b
        parent[root_b] = root_a
        del paths[root_b]

    return list(paths.values())


def _dist(a: Pt, b: Pt) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


def _pair_opposing_arms(
    chains: list[dict[str, Any]], arms: list[tuple[int, bool]]
) -> list[tuple[tuple[int, bool], tuple[int, bool]]]:
    """Greedily pair junction arms whose leave-directions oppose."""
    directions = {arm: _arm_direction(chains, arm) for arm in arms}
    used: set[int] = set()
    pairs: list[tuple[tuple[int, bool], tuple[int, bool]]] = []
    for i, arm_a in enumerate(arms):
        if i in used:
            continue
        best_j, best_angle = None, THROUGH_MIN_ANGLE_DEG
        for j in range(i + 1, len(arms)):
            if j in used:
                continue
            angle = _angle_between(directions[arm_a], directions[arms[j]])
            if angle >= best_angle:
                best_angle, best_j = angle, j
        if best_j is not None:
            used.update((i, best_j))
            pairs.append((arm_a, arms[best_j]))
    return pairs


def _arm_point(
    chains: list[dict[str, Any]], arm: tuple[int, bool], *, at_node: bool
) -> Pt:
    points = chains[arm[0]]["points"]
    return points[0] if arm[1] == at_node else points[-1]


def _arm_direction(chains: list[dict[str, Any]], arm: tuple[int, bool]) -> float:
    """Direction (radians) of the arm LEAVING the junction."""
    points = chains[arm[0]]["points"]
    if arm[1]:
        a, b = points[0], points[min(4, len(points) - 1)]
    else:
        a, b = points[-1], points[max(-5, -len(points))]
    return atan2(b[1] - a[1], b[0] - a[0])


def _angle_between(direction_a: float, direction_b: float) -> float:
    dot = cos(direction_a) * cos(direction_b) + sin(direction_a) * sin(direction_b)
    return degrees(abs(atan2((1 - dot * dot) ** 0.5, dot)))


# ── Gap bridging ────────────────────────────────────────────────────────────


def _bridge_chain_gaps(
    chains: list[list[Pt]], text_boxes_px: list[Box]
) -> list[list[Pt]]:
    """Join near-collinear chain ends across dashes, dots, and label holes."""
    paths: list[list[Pt] | None] = [list(c) for c in chains]
    changed = True
    while changed:
        changed = False
        for i in range(len(paths)):
            if paths[i] is None:
                continue
            for j in range(i + 1, len(paths)):
                if paths[j] is None:
                    continue
                path_i = paths[i]
                path_j = paths[j]
                assert path_i is not None and path_j is not None
                joined = _try_join(path_i, path_j, text_boxes_px)
                if joined is not None:
                    paths[i] = joined
                    paths[j] = None
                    changed = True
        paths = [p for p in paths if p is not None]
    return [path for path in paths if path is not None]


def _try_join(
    path_a: list[Pt], path_b: list[Pt], text_boxes_px: list[Box]
) -> list[Pt] | None:
    for a_end in (False, True):
        for b_start in (True, False):
            pa = path_a if a_end else path_a[::-1]
            pb = path_b if b_start else path_b[::-1]
            tail, head = pa[-1], pb[0]
            gap = hypot(head[0] - tail[0], head[1] - tail[1])
            limit = BRIDGE_GAP_PX
            if gap > limit:
                mid = ((tail[0] + head[0]) / 2, (tail[1] + head[1]) / 2)
                if gap <= BRIDGE_TEXT_GAP_PX and _in_any_box(mid, text_boxes_px):
                    limit = BRIDGE_TEXT_GAP_PX
            if gap > limit or gap < 1e-9:
                continue
            out_dir = _tail_direction(pa)
            jump_dir = atan2(head[1] - tail[1], head[0] - tail[0])
            in_dir = _tail_direction(pb[::-1])
            if (
                _angle_between(out_dir, jump_dir) <= BRIDGE_MAX_ANGLE_DEG
                and _angle_between(jump_dir, atan2(-sin(in_dir), -cos(in_dir)))
                <= BRIDGE_MAX_ANGLE_DEG
            ):
                return pa + pb
    return None


def _tail_direction(path: list[Pt]) -> float:
    """Direction of travel arriving at the path's last point."""
    a = path[max(-5, -len(path))]
    b = path[-1]
    return atan2(b[1] - a[1], b[0] - a[0])


def _in_any_box(point: Pt, boxes: list[Box]) -> bool:
    px, py = point
    return any(x0 <= px <= x1 and y0 <= py <= y1 for x0, y0, x1, y1 in boxes)


# ── Output shaping ──────────────────────────────────────────────────────────


def _simplify(cv2: Any, np: Any, chain: list[Pt]) -> list[Pt]:
    points = np.array(chain, np.float32).reshape(-1, 1, 2)
    approx = cv2.approxPolyDP(points, SIMPLIFY_EPS_PX, False).reshape(-1, 2)
    return [(float(x), float(y)) for x, y in approx]


def _polyline_length(points: list[Any]) -> float:
    total = 0.0
    for index in range(len(points) - 1):
        ax, ay = points[index][0], points[index][1]
        bx, by = points[index + 1][0], points[index + 1][1]
        total += hypot(bx - ax, by - ay)
    return total


def _is_page_frame(points: list[Pt], width_px: int, height_px: int) -> bool:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (max(xs) - min(xs)) > MAX_WIRE_SPAN_FRAC * width_px or (
        max(ys) - min(ys)
    ) > MAX_WIRE_SPAN_FRAC * height_px


# ── OCR labels (shared with the extractor) ──────────────────────────────────

#: Minimum recognition confidence for an OCR label to be trusted.
OCR_MIN_CONFIDENCE = 0.5

_OCR_ENGINE: Any = None


def _ocr_engine() -> Any:
    """Lazily build one RapidOCR engine per process (model load is slow)."""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr import EngineType, RapidOCR

        _OCR_ENGINE = RapidOCR(
            params={
                "Det.engine_type": EngineType.TORCH,
                "Cls.engine_type": EngineType.TORCH,
                "Rec.engine_type": EngineType.TORCH,
            }
        )
    return _OCR_ENGINE


def ocr_text_labels(
    page_png: bytes,
    *,
    page_size_pt: tuple[float, float],
) -> list[tuple[float, float, float, float, str]]:
    """OCR the page render into positioned text labels (page-pt space).

    Scanned drawings' embedded text layers are often unusable for the
    rotated wire ids printed along vertical wires; PP-OCR's detector reads
    them natively. Used to NAME traced nets (never to mask — masking uses
    the deterministic PDF layer). Returns ``[]`` on any failure: OCR is an
    enhancement, never a requirement.
    """
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(page_png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return []
        height_px, width_px = image.shape[:2]
        page_w_pt, page_h_pt = page_size_pt
        if page_w_pt <= 0 or page_h_pt <= 0:
            return []
        result = _ocr_engine()(image)
    except Exception as error:
        _log.warning("OCR label pass failed: %s", error)
        return []

    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None) or []
    scores = getattr(result, "scores", None) or []
    if boxes is None or len(boxes) == 0:
        return []

    scale_x, scale_y = width_px / page_w_pt, height_px / page_h_pt
    labels: list[tuple[float, float, float, float, str]] = []
    for index, quad in enumerate(boxes):
        text = str(texts[index]) if index < len(texts) else ""
        score = float(scores[index]) if index < len(scores) else 1.0
        if not text.strip() or score < OCR_MIN_CONFIDENCE:
            continue
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        labels.append(
            (
                min(xs) / scale_x,
                min(ys) / scale_y,
                max(xs) / scale_x,
                max(ys) / scale_y,
                text.strip(),
            )
        )
    return labels
