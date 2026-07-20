"""Pin designators for multi-pin components: text layer first, vision second.

The remaining connectivity gap after 2-terminal pin assignment is the
multi-pin devices (ICs, displays, transistors, multi-way switches) — their
pin identities cannot be guessed, but they ARE printed on the drawing next
to each symbol pin. Two evidence-based tiers recover them:

1. **Text layer** (vector PDFs, deterministic): the pin labels are real PDF
   text with exact coordinates, and the graph stores the exact attachment
   point where each traced wire meets the component. The label nearest to an
   attachment (within a tight radius, pin-token grammar) IS that pin's
   designator. ``pinSource: "text-layer"``.
2. **Vision crops** (scans / sparse text): crop the component from the page
   render, draw a numbered marker at each unpinned attachment point, and ask
   the vision model to transcribe the printed pin label at each marker —
   transcription of visible print, never recall. ``pinSource: "vision"``.

Both tiers only fill nulls — drawing/model/assigned pins are never
overwritten — and both record their provenance, so exports and auditors can
tell measured identities from assigned bookkeeping.
"""

from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

#: A plausible printed pin label: "14", "7", "VDD", "GND", "RA0", "Q7", "CLK".
PIN_TOKEN_RE = re.compile(r"^(?:\d{1,2}|[A-Z]{1,5}\d{0,2}|[A-Z]\d[A-Z])$")

#: Max distance (page pt) between an attachment point and its pin label.
TEXT_PIN_RADIUS_PT = 14.0
#: Crop padding fraction around the component bbox for vision reading.
_CROP_PAD_FRAC = 0.3
_MAX_CROP_SIZE = 768
#: Cap vision calls per page so a dense sheet can't run away on cost.
MAX_VISION_COMPONENTS_PER_PAGE = 24

_VISION_SYSTEM = (
    "You are a transcription engine for engineering-drawing crops. You copy "
    "printed text exactly as shown; you never infer, complete, or recall "
    "part data from prior knowledge. Output JSON only."
)

_VISION_PROMPT = (
    "This crop shows ONE schematic component. {count} red numbered markers "
    "sit exactly where wires attach to the component symbol. For EACH marker, "
    "transcribe the pin number or pin name printed nearest to that marker, "
    'at the symbol\'s edge (e.g. "14", "VDD", "RA0", "Q7"). Rules:\n'
    "- TRANSCRIBE only what is printed; if no pin label is visible at a "
    "marker, use null.\n"
    "- Pin labels are the small tokens at the symbol boundary, not the "
    "component's reference designator or value.\n"
    'Return JSON: {{"pins": [{{"marker": 1, "pin": "14"}}, ...]}} with one '
    "entry per marker."
)


def assign_pins_from_text(
    graph: dict[str, Any],
    text_labels_by_page: dict[int, list[tuple[float, float, float, float, str]]],
) -> int:
    """Fill unpinned net memberships from the page text layer, in place.

    Returns the number of memberships that gained a pin.
    """
    components = _components_by_id(graph)
    assigned = 0
    claimed: dict[str, set[int]] = {}  # component id -> claimed label indexes
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        page_no = int(net.get("page") or 1)
        labels = text_labels_by_page.get(page_no) or []
        if not labels:
            continue
        for node in net.get("nodes") or []:
            if not isinstance(node, dict) or node.get("pin"):
                continue
            attachment = node.get("attachment")
            component = components.get(str(node.get("component")))
            if component is None or not (
                isinstance(attachment, (list, tuple)) and len(attachment) == 2
            ):
                continue
            taken = claimed.setdefault(str(component.get("id")), set())
            best = _nearest_pin_label(
                (float(attachment[0]), float(attachment[1])),
                labels,
                component,
                taken,
            )
            if best is None:
                continue
            label_index, token = best
            taken.add(label_index)
            node["pin"] = token
            node["pinSource"] = "text-layer"
            assigned += 1
    return assigned


def assign_pins_with_vision(
    graph: dict[str, Any],
    page_images: list[tuple[int, bytes]],
    *,
    understand: Any,
) -> int:
    """Vision-crop pin reading for memberships the text layer couldn't fill.

    ``understand(prompt, system, png_bytes) -> dict`` is the cached model
    call. Never raises; returns memberships that gained a pin.
    """
    png_by_page = dict(page_images)
    components = _components_by_id(graph)
    # component id -> [(net node, attachment)] still unpinned, grouped per page.
    pending: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for net in graph.get("nets") or []:
        if not isinstance(net, dict):
            continue
        page_no = int(net.get("page") or 1)
        for node in net.get("nodes") or []:
            if not isinstance(node, dict) or node.get("pin"):
                continue
            comp_id = str(node.get("component") or "")
            component = components.get(comp_id)
            attachment = node.get("attachment")
            if component is None or component.get("bbox") is None:
                continue
            if not (isinstance(attachment, (list, tuple)) and len(attachment) == 2):
                continue
            pending.setdefault((page_no, comp_id), []).append(node)

    assigned = 0
    calls_per_page: dict[int, int] = {}
    for (page_no, comp_id), nodes in sorted(
        pending.items(), key=lambda item: -len(item[1])
    ):
        if calls_per_page.get(page_no, 0) >= MAX_VISION_COMPONENTS_PER_PAGE:
            continue
        png_bytes = png_by_page.get(page_no)
        component = components[comp_id]
        page_size = _page_size_pt(graph, page_no)
        if not (png_bytes and page_size):
            continue
        try:
            crop_png, marker_count = _marked_crop(
                png_bytes, component, nodes, page_size
            )
            if crop_png is None:
                continue
            calls_per_page[page_no] = calls_per_page.get(page_no, 0) + 1
            response = understand(
                _VISION_PROMPT.format(count=marker_count), _VISION_SYSTEM, crop_png
            )
            assigned += _apply_vision_pins(nodes, response)
        except Exception as error:
            _log.warning("Vision pin reading failed for %s: %s", comp_id, error)
    return assigned


def _components_by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(c.get("id")): c
        for c in graph.get("components") or []
        if isinstance(c, dict)
    }


def _nearest_pin_label(
    attachment: tuple[float, float],
    labels: list[tuple[float, float, float, float, str]],
    component: dict[str, Any],
    taken: set[int],
) -> tuple[int, str] | None:
    """The closest plausible pin token to the attachment, if any.

    Excludes the component's own identity strings (refDes/value) so "Q7"
    the designator never becomes "Q7" the pin.
    """
    own = {
        str(component.get(field) or "").strip().upper()
        for field in ("refDes", "value", "partNumber")
    }
    ax, ay = attachment
    best: tuple[float, int, str] | None = None
    for index, (x0, y0, x1, y1, text) in enumerate(labels):
        if index in taken:
            continue
        token = text.strip()
        if not PIN_TOKEN_RE.match(token) or token.upper() in own:
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        distance = ((cx - ax) ** 2 + (cy - ay) ** 2) ** 0.5
        if distance > TEXT_PIN_RADIUS_PT:
            continue
        if best is None or distance < best[0]:
            best = (distance, index, token)
    return (best[1], best[2]) if best else None


def _page_size_pt(graph: dict[str, Any], page_no: int) -> tuple[float, float] | None:
    for page in graph.get("pages") or []:
        if (
            isinstance(page, dict)
            and int(page.get("pageNumber") or page.get("page") or 0) == page_no
        ):
            width = float(page.get("width") or 0)
            height = float(page.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
    return None


def _marked_crop(
    page_png: bytes,
    component: dict[str, Any],
    nodes: list[dict[str, Any]],
    page_size_pt: tuple[float, float],
) -> tuple[bytes | None, int]:
    """Crop the component (padded) and draw numbered markers at attachments."""
    from PIL import Image, ImageDraw

    page = Image.open(io.BytesIO(page_png)).convert("RGB")
    page_w_pt, page_h_pt = page_size_pt
    sx, sy = page.width / page_w_pt, page.height / page_h_pt

    x0, y0, x1, y1 = (float(v) for v in component["bbox"])
    pad_x = max((x1 - x0) * _CROP_PAD_FRAC, 8.0)
    pad_y = max((y1 - y0) * _CROP_PAD_FRAC, 8.0)
    box = (
        max(0, int((x0 - pad_x) * sx)),
        max(0, int((y0 - pad_y) * sy)),
        min(page.width, int((x1 + pad_x) * sx)),
        min(page.height, int((y1 + pad_y) * sy)),
    )
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None, 0
    crop = page.crop(box)
    draw = ImageDraw.Draw(crop)
    radius = max(4, int(min(crop.width, crop.height) * 0.02))
    for marker, node in enumerate(nodes, start=1):
        ax, ay = node["attachment"]
        px = float(ax) * sx - box[0]
        py = float(ay) * sy - box[1]
        draw.ellipse(
            (px - radius, py - radius, px + radius, py + radius),
            outline=(220, 0, 0),
            width=2,
        )
        draw.text((px + radius + 1, py - radius), str(marker), fill=(220, 0, 0))
    if crop.width > _MAX_CROP_SIZE or crop.height > _MAX_CROP_SIZE:
        scale = min(_MAX_CROP_SIZE / crop.width, _MAX_CROP_SIZE / crop.height)
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), len(nodes)


def _apply_vision_pins(nodes: list[dict[str, Any]], response: Any) -> int:
    """Validate + fold the model's marker→pin transcriptions into the nodes."""
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return 0
    entries = response.get("pins") if isinstance(response, dict) else None
    if not isinstance(entries, list):
        return 0
    assigned = 0
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            marker_value = entry.get("marker")
            if marker_value is None:
                continue
            marker = int(marker_value)
        except (TypeError, ValueError):
            continue
        token = str(entry.get("pin") or "").strip()
        if not (1 <= marker <= len(nodes)) or not PIN_TOKEN_RE.match(token):
            continue
        if token in seen:
            continue  # one pin label cannot serve two attachments
        node = nodes[marker - 1]
        if node.get("pin"):
            continue
        seen.add(token)
        node["pin"] = token
        node["pinSource"] = "vision"
        assigned += 1
    return assigned
