"""Per-component value / part-number recovery from isolated crops.

The whole-page model pass only enumerates the components it judges
significant (~20 on a dense sheet); the detection passes box every remaining
symbol but carry no ``value`` or ``partNumber``. That is the structural cause
of the "most components have a null value" gap measured on real drawings
(e.g. the Lixie Clock sheet: 60 of 80 components valueless, all of them
detection-only boxes).

This pass closes the gap deterministically in orchestration but
model-driven in understanding: every component that already has a page-pt
bounding box but is missing its printed ``value`` (and, for significant
parts, its ``partNumber``) is re-read from a CROP of just that component —
verbatim transcription, never recalled from prior knowledge, exactly like
:mod:`label_verify` and :mod:`pin_identification`. It only fills nulls and
records provenance (``valueSource`` / ``partNumberSource = "vision-crop"``),
so a measured value is always distinguishable from a whole-page guess.

A separate, model-free :func:`reconcile_value_part_number` cleans up the
contradiction the whole-page pass leaves on ICs/MCUs, where ``value`` names a
different device family than the (verified) ``partNumber`` — the value is a
stale guess; it is moved aside under ``value_was`` so the audit trail keeps
it.
"""

from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import Any

from docling_serve.schematic.label_verify import _plausible_part_number

_log = logging.getLogger(__name__)

#: DPI to re-render the source PDF page regions at for value crops. The
#: model-facing whole-page render is downscaled under a few MB (~150 dpi for a
#: large sheet), which loses the decimal point in a tiny "5.1k". Cropping from
#: a dedicated high-DPI page render keeps small value text legible. Override
#: with DOCLING_SCHEMATIC_VALUE_CROP_DPI.
_HIRES_DPI_ENV = "DOCLING_SCHEMATIC_VALUE_CROP_DPI"
_DEFAULT_HIRES_DPI = 400

#: Cap crops per extraction so a very dense sheet can't run away on model
#: cost. Override with DOCLING_SCHEMATIC_MAX_VALUE_CROPS (responses are cached,
#: so the steady-state cost of a re-ingest is zero regardless).
_MAX_VALUE_CROPS_ENV = "DOCLING_SCHEMATIC_MAX_VALUE_CROPS"
_DEFAULT_MAX_VALUE_CROPS = 120

_CROP_PAD_FRAC = 0.35
_MAX_CROP_SIZE = 512

#: Component-type tokens whose printed identity is a manufacturer part number
#: worth recovering (not just a passive value).
_PART_BEARING_TOKENS = (
    "ic", "mcu", "microcontroller", "regulator", "transistor", "mosfet",
    "fet", "relay", "switch", "connector", "diode", "led", "buzzer",
    "crystal", "oscillator", "display", "tube", "op-amp", "opamp", "array",
)

VALUE_SYSTEM_PROMPT = (
    "You are a transcription engine for engineering-drawing crops. You copy "
    "the printed text EXACTLY as it appears — never inferring, completing, or "
    "substituting from knowledge of electronic devices. You always answer "
    "with a single valid JSON object and nothing else."
)

VALUE_USER_PROMPT = (
    "This image is a crop showing ONE component from an engineering "
    "schematic. Transcribe the small text printed next to its symbol.\n"
    "Return ONLY a JSON object:\n"
    '{"refDes": str|null, "value": str|null, "partNumber": str|null}\n'
    "Rules:\n"
    "- value is the component VALUE printed by the symbol: a resistance, "
    "capacitance, inductance, or rating such as 5.1k, 330, 100nF, 4.7uF, 82, "
    "270, 0.1, 1M. Copy it verbatim. Null if no value is printed.\n"
    "- partNumber is a manufacturer part number printed by the symbol (e.g. "
    "MMPQ3904, LT1117-3.3, PS1023ABLK) — usually the longest alphanumeric "
    "string; null when not printed.\n"
    "- refDes is the reference designator (e.g. R26, U3, SW2); it always "
    "BEGINS WITH LETTERS. Null if not legible.\n"
    "- TRANSCRIBE ONLY what is fully visible in THIS crop. Never guess a "
    "value or part number from what the device looks like; use null instead.\n"
    "- Text from NEIGHBOURING components may intrude at the crop edges; "
    "ignore anything not attached to the central component."
)

#: A passive/printed value: digit-led, optional decimal, optional SI
#: multiplier and unit (5.1k, 330, 100nF, 4.7uF, 22R, 1k5, 0.1, 82, 270).
_VALUE_RE = re.compile(
    r"^[0-9]+(?:[.,][0-9]+)?\s*"  # leading number
    r"[a-zA-ZµμΩ%/.\-0-9]*$"      # optional multiplier/unit/suffix
)
#: A bare reference designator the model may echo into the value field.
_REFDES_RE = re.compile(r"^[A-Z]{1,4}[0-9]{1,4}[A-Z]?$")


class _HiResPages:
    """Lazily renders source-PDF pages at high DPI for sharp crops.

    Each page is rendered once (without the model-payload size cap) and reused
    for every crop on it. Falls back to ``None`` so the caller drops back to
    the model-facing render. Pure best-effort: any failure yields ``None``.
    """

    def __init__(self, source_path: Path | None, *, dpi: int | None = None) -> None:
        self._source = source_path
        self._dpi = dpi or _configured_hires_dpi()
        self._cache: dict[int, Any] = {}
        self._pdf: Any = None
        self._unavailable = source_path is None or source_path.suffix.lower() != ".pdf"

    def page(self, page_no: int) -> Any | None:
        if self._unavailable:
            return None
        if page_no in self._cache:
            return self._cache[page_no]
        image = self._render(page_no)
        self._cache[page_no] = image
        return image

    def _render(self, page_no: int) -> Any | None:
        try:
            import pypdfium2 as pdfium
        except ImportError:  # pragma: no cover
            self._unavailable = True
            return None
        try:
            if self._pdf is None:
                self._pdf = pdfium.PdfDocument(str(self._source))
            index = page_no - 1
            if not (0 <= index < len(self._pdf)):
                return None
            page = self._pdf[index]
            bitmap = page.render(scale=self._dpi / 72.0)
            return bitmap.to_pil().convert("RGB")
        except Exception as error:  # pragma: no cover - environment dependent
            _log.warning("Hi-res page render failed for page %s: %s", page_no, error)
            self._unavailable = True
            return None


def recover_component_identity(
    graph: dict[str, Any],
    page_images: list[tuple[int, bytes]],
    *,
    understand: Any,
    source_path: Path | None = None,
    max_crops: int | None = None,
) -> dict[str, int]:
    """Fill missing component values/part numbers from per-component crops.

    ``understand(prompt, system, png_bytes) -> dict`` is the cached model
    call. ``source_path`` (the original PDF) is re-rendered at high DPI for
    sharp small-text crops; without it crops come from the model-facing page
    render. Never raises. Returns ``{"values": n, "partNumbers": m}``.
    """
    png_by_page = dict(page_images)
    hires = _HiResPages(source_path)
    if max_crops is None:
        max_crops = _configured_cap()

    candidates = [
        component
        for component in graph.get("components") or []
        if isinstance(component, dict)
        and component.get("bbox")
        and _needs_identity(component)
    ]
    # Significant parts (a missing part number matters most) first, then the
    # passives whose value is still null.
    candidates.sort(key=lambda c: (not _is_part_bearing(c), str(c.get("id"))))

    filled = {"values": 0, "partNumbers": 0}
    crops_done = 0
    for component in candidates:
        if crops_done >= max_crops:
            break
        page_no = int(component.get("page") or 1)
        page_size = _page_size_pt(graph, page_no)
        if not page_size:
            continue
        hires_page = hires.page(page_no)
        if hires_page is not None:
            crop_png = _crop_from_image(hires_page, component, page_size)
        else:
            png_bytes = png_by_page.get(page_no)
            crop_png = (
                _crop_component(png_bytes, component, page_size) if png_bytes else None
            )
        if crop_png is None:
            continue
        crops_done += 1
        try:
            payload = understand(VALUE_USER_PROMPT, VALUE_SYSTEM_PROMPT, crop_png)
        except Exception as error:  # a bad crop must never fail the job
            _log.warning("Value crop read failed for %s: %s", component.get("refDes"), error)
            continue
        if not isinstance(payload, dict):
            continue
        if _apply_value(component, payload):
            filled["values"] += 1
        if _apply_part_number(component, payload):
            filled["partNumbers"] += 1
    if any(filled.values()):
        _log.info(
            "component identity recovery: %s value(s), %s part number(s) from %s crops",
            filled["values"], filled["partNumbers"], crops_done,
        )
    return filled


def reconcile_value_part_number(graph: dict[str, Any]) -> int:
    """Drop a ``value`` that contradicts a verified part number, in place.

    On ICs/MCUs the whole-page pass routinely fills ``value`` with a guess at
    the device family (e.g. ``ULN2803A``) while the focused crop pass recovers
    the real ``partNumber`` (``PIC16LF1719-I/PT-ND``). The two then disagree.
    For a part-bearing component whose ``value`` itself looks like a part
    number and differs from the resolved ``partNumber``, the value is the
    stale guess: move it to ``value_was`` and null ``value``. Returns how many
    components were cleaned.
    """
    cleaned = 0
    for component in graph.get("components") or []:
        if not isinstance(component, dict) or not _is_part_bearing(component):
            continue
        value = _text(component.get("value"))
        part = _text(component.get("partNumber"))
        if not (value and part):
            continue
        if _norm_part(value) == _norm_part(part):
            continue
        # Only act when the VALUE looks like a part number, not a real value
        # like 10k / 330 (those legitimately coexist with a part number).
        if not _plausible_part_number(value) or _looks_like_value(value):
            continue
        component["value_was"] = value
        component["value"] = None
        component["reviewNote"] = (
            "value named a different part than partNumber; cleared as a stale "
            "whole-page guess"
        )
        cleaned += 1
    return cleaned


def _needs_identity(component: dict[str, Any]) -> bool:
    # Missing a printed value, or a part-bearing component missing its part
    # number — either gap is worth a focused crop read.
    if not component.get("value"):
        return True
    return _is_part_bearing(component) and not component.get("partNumber")


def _is_part_bearing(component: dict[str, Any]) -> bool:
    ctype = str(component.get("type") or "").lower()
    return any(token in ctype for token in _PART_BEARING_TOKENS)


def _apply_value(component: dict[str, Any], payload: dict[str, Any]) -> bool:
    if component.get("value"):
        return False
    value = _text(payload.get("value"))
    if not value or not _plausible_value(value, component):
        return False
    component["value"] = value
    component["valueSource"] = "vision-crop"
    return True


def _apply_part_number(component: dict[str, Any], payload: dict[str, Any]) -> bool:
    if component.get("partNumber") or not _is_part_bearing(component):
        return False
    part = _text(payload.get("partNumber"))
    own_ref = str(component.get("refDes") or "").strip().upper()
    if not part or part.upper() == own_ref or not _plausible_recovered_part(part):
        return False
    component["partNumber"] = part
    component["partNumberSource"] = "vision-crop"
    return True


def _plausible_recovered_part(part: str) -> bool:
    """A crop-transcribed part number worth keeping.

    Accepts the conservative shared rule, OR a longer mixed alphanumeric token
    (e.g. ``MMPQ3904``) that the shared rule rejects only because 4 letters +
    4 digits superficially matches a reference designator — real refDes carry
    far fewer characters.
    """
    if _plausible_part_number(part):
        return True
    letters = sum(ch.isalpha() for ch in part)
    digits = sum(ch.isdigit() for ch in part)
    return len(part) >= 6 and letters >= 2 and digits >= 3


def _plausible_value(value: str, component: dict[str, Any]) -> bool:
    """A transcribed value worth keeping: not the refDes, not too long."""
    if len(value) > 32:
        return False
    own_ref = str(component.get("refDes") or "").strip().upper()
    if value.upper() == own_ref or _REFDES_RE.match(value.upper()):
        return False
    return True


def _looks_like_value(text: str) -> bool:
    return bool(text) and bool(_VALUE_RE.match(text.strip()))


def _crop_component(
    page_png: bytes,
    component: dict[str, Any],
    page_size_pt: tuple[float, float],
) -> bytes | None:
    """Crop the component (padded) from the model-facing page render bytes."""
    from PIL import Image

    try:
        page = Image.open(io.BytesIO(page_png)).convert("RGB")
    except Exception:
        return None
    return _crop_from_image(page, component, page_size_pt)


def _crop_from_image(
    page: Any,
    component: dict[str, Any],
    page_size_pt: tuple[float, float],
) -> bytes | None:
    """Crop the component (padded) from an already-decoded PIL page image."""
    page_w_pt, page_h_pt = page_size_pt
    if page_w_pt <= 0 or page_h_pt <= 0:
        return None
    sx, sy = page.width / page_w_pt, page.height / page_h_pt
    try:
        x0, y0, x1, y1 = (float(v) for v in component["bbox"])
    except (TypeError, ValueError):
        return None
    pad_x = max((x1 - x0) * _CROP_PAD_FRAC, 10.0)
    pad_y = max((y1 - y0) * _CROP_PAD_FRAC, 10.0)
    box = (
        max(0, int((x0 - pad_x) * sx)),
        max(0, int((y0 - pad_y) * sy)),
        min(page.width, int((x1 + pad_x) * sx)),
        min(page.height, int((y1 + pad_y) * sy)),
    )
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    crop = page.crop(box)
    if crop.width > _MAX_CROP_SIZE or crop.height > _MAX_CROP_SIZE:
        scale = min(_MAX_CROP_SIZE / crop.width, _MAX_CROP_SIZE / crop.height)
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _page_size_pt(graph: dict[str, Any], page_no: int) -> tuple[float, float] | None:
    for page in graph.get("pages") or []:
        if isinstance(page, dict) and int(
            page.get("pageNumber") or page.get("page") or 0
        ) == page_no:
            width = float(page.get("width") or 0)
            height = float(page.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
    return None


def _configured_cap() -> int:
    raw = (os.environ.get(_MAX_VALUE_CROPS_ENV) or "").strip()
    if raw.isdigit():
        return int(raw)
    return _DEFAULT_MAX_VALUE_CROPS


def _configured_hires_dpi() -> int:
    raw = (os.environ.get(_HIRES_DPI_ENV) or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_HIRES_DPI


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm_part(value: Any) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    return normalized or None
