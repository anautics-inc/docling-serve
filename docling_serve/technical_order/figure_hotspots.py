"""Clickable callout detection on rendered figure sheets.

IPB exploded-view figures label every part with an index callout ("1", "14",
"5A"). Tesseract sparse-text mode (PSM 11) reads those labels WITH pixel
boxes from the rendered sheet — pure Python/OCR, no LLM needed, and it works
identically for born-digital and scanned documents because it reads pixels.

Detected hotspots are normalized to page fractions and validated against the
figure's actual index set (from the parsed MPL), which kills page numbers,
TO numbers, and drawing-geometry noise. The UI overlays them as click
targets: image callout → part, part → callout.
"""

from __future__ import annotations

import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from docling_serve.technical_order.contract import provenance
from docling_serve.technical_order.mpl import FigureRecord, PartsListEntry

#: Min tesseract confidence. Kept modest because the real noise filter is index-
#: set membership (a token must BE one of the figure's parsed callouts) plus the
#: glyph-size band — so we can accept fainter true callouts for better recall.
_MIN_CONFIDENCE = 40.0
#: Minimum normalized box half-extent so a single-glyph callout is still a usable
#: click target (and a sane anchor for re-stamping a callout when a part changes).
_MIN_BOX_HALF = 0.009
#: Callout glyph-height band as a FRACTION of page height (DPI-independent):
#: index callouts print ~0.5%-3.5% of the sheet height; below is hairline/leader
#: noise, above is figure titles / banners.
_MIN_GLYPH_FRAC = 0.004
_MAX_GLYPH_FRAC = 0.040
#: Render DPI for figure sheets. 200 DPI is the sweet spot: callouts are legible
#: and a 50-figure doc OCRs in ~1 min. (300 DPI + binarization measured WORSE +
#: 2x slower on real drawings — Otsu wipes anti-aliased callouts on shaded art.)
_FIGURE_DPI = 200
#: tesseract page-segmentation modes unioned for callout recall: 11 = sparse
#: text (no layout), 12 = sparse text + OSD. Different segmenters catch callouts
#: the other drops (rotated dimension labels, glyphs touching leader lines).
_OCR_PSMS = (11, 12)
#: Two detections of the SAME index closer than this (page fraction) are the
#: same callout double-read; keep the higher-confidence one.
_DEDUP_DIST = 0.02

#: A bare index callout: digits, optional dash group, optional letter suffix
#: ("1", "14", "5A", "6-1", "12B"). Anchored so "1981" / TO numbers never match.
_CALLOUT_TOKEN = re.compile(r"^\d{1,3}(?:-\d{1,3})?[A-Z]?$")


@dataclass(slots=True)
class FigureHotspot:
    index: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float
    # Bidirectional link to the parts list — filled by link_hotspots_to_parts:
    # the callout's part. ``None`` until linked.
    part_sequence: int | None = None
    part_number: str = ""
    detection_method: str = "tesseract-ocr"

    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    def as_dict(self) -> dict:
        confidence = (
            self.confidence / 100.0 if self.confidence > 1.0 else self.confidence
        )
        normalized_confidence = round(max(0.0, min(1.0, confidence)), 4)
        return {
            "index": self.index,
            "box": [self.x0, self.y0, self.x1, self.y1],
            "confidence": normalized_confidence,
            "partSequence": self.part_sequence,
            "partNumber": self.part_number,
            "provenance": provenance(
                method=self.detection_method,
                parser="docling-serve.technical-order.figure-hotspots",
                version="2",
                confidence=normalized_confidence,
                geometry={
                    "coordinateSystem": "normalized-page-top-left",
                    "boundingBox": [self.x0, self.y0, self.x1, self.y1],
                },
            ),
        }


def png_dimensions(png_path: Path) -> tuple[int, int]:
    """Width/height from the PNG IHDR header (no imaging dependency)."""
    with png_path.open("rb") as fh:
        header = fh.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {png_path}")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def render_figure_png(
    pdf_path: Path, page_number: int, out_stem: Path, *, dpi: int = _FIGURE_DPI
) -> Path | None:
    """Render one PDF page to a PNG via poppler ``pdftoppm`` (no imaging dep).

    ``out_stem`` is the path WITHOUT extension; ``-singlefile`` makes the output
    deterministically ``{out_stem}.png``. Returns the PNG path, or ``None`` if
    poppler is unavailable or the render fails.
    """
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "pdftoppm",
                "-png",
                "-r",
                str(dpi),
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
                str(pdf_path),
                str(out_stem),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None
    png = out_stem.with_suffix(".png")
    return png if png.is_file() else None


def render_figure_svg(pdf_path: Path, page_number: int, out_stem: Path) -> Path | None:
    """Vector "digital twin" of one page via ``pdftocairo -svg``.

    For a born-digital drawing this reproduces the exact line art (the same
    export the schematic extractor vectorizes), so viewers can zoom losslessly
    and downstream 2D→3D stages get true geometry instead of raster pixels.
    Returns the SVG path, or ``None`` if poppler is unavailable/fails.
    """
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    svg = out_stem.with_suffix(".svg")
    try:
        subprocess.run(
            [
                "pdftocairo",
                "-svg",
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                str(pdf_path),
                str(svg),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None
    return svg if svg.is_file() else None


def _tesseract_tokens(
    png_path: Path, psm: int
) -> list[tuple[str, float, int, int, int, int]]:
    """Run tesseract once; return (text, conf, x, y, w, h) rows. Empty on failure.

    No char whitelist: the index-set membership check downstream is the real
    filter, and a whitelist measurably hurt tesseract 4.x segmentation here.
    """
    try:
        out = subprocess.run(
            ["tesseract", str(png_path), "-", "--psm", str(psm), "tsv"],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return []
    rows: list[tuple[str, float, int, int, int, int]] = []
    for line in out.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 12:
            continue
        text = fields[11].strip().upper().rstrip(".,")
        if not text:
            continue
        try:
            conf = float(fields[10])
            x, y, w, h = (int(fields[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        rows.append((text, conf, x, y, w, h))
    return rows


def detect_figure_hotspots(
    png_path: Path, valid_indices: set[str]
) -> list[FigureHotspot]:
    """Locate index callouts on a rendered figure sheet.

    Binarizes the sheet, then unions multiple tesseract page-segmentation passes
    for recall. Only tokens that exist in the figure's parsed index set survive —
    the MPL is the source of truth for which callouts the figure carries. Near-
    duplicate detections collapse to the highest-confidence box; genuinely
    separate placements (a part called out twice) are kept.
    """
    if not valid_indices:
        return []
    try:
        width, height = png_dimensions(png_path)
    except (OSError, ValueError):
        return []
    if width <= 0 or height <= 0:
        return []

    min_h = _MIN_GLYPH_FRAC * height
    max_h = _MAX_GLYPH_FRAC * height
    wanted = {token.strip().upper() for token in valid_indices if token.strip()}
    raw: list[FigureHotspot] = []
    for psm in _OCR_PSMS:
        for text, confidence, x, y, w, h in _tesseract_tokens(png_path, psm):
            if not _CALLOUT_TOKEN.match(text) or text not in wanted:
                continue
            if confidence < _MIN_CONFIDENCE:
                continue
            if not (min_h <= h <= max_h):
                continue
            # Pad to a minimum click target around the glyph centre — a bare "1"
            # is only a few px wide, unusable as a UI hotspot or re-stamp anchor.
            cx = (x + w / 2) / width
            cy = (y + h / 2) / height
            half_w = max(w / (2 * width), _MIN_BOX_HALF)
            half_h = max(h / (2 * height), _MIN_BOX_HALF)
            raw.append(
                FigureHotspot(
                    index=text,
                    x0=round(max(0.0, cx - half_w), 4),
                    y0=round(max(0.0, cy - half_h), 4),
                    x1=round(min(1.0, cx + half_w), 4),
                    y1=round(min(1.0, cy + half_h), 4),
                    confidence=confidence,
                )
            )
    return _dedup(raw)


def _dedup(hotspots: list[FigureHotspot]) -> list[FigureHotspot]:
    """Collapse near-duplicate detections of the same index (keep best conf)."""
    kept: list[FigureHotspot] = []
    for hs in sorted(hotspots, key=lambda h: -h.confidence):
        cx, cy = hs.center()
        if any(
            k.index == hs.index
            and abs(k.center()[0] - cx) < _DEDUP_DIST
            and abs(k.center()[1] - cy) < _DEDUP_DIST
            for k in kept
        ):
            continue
        kept.append(hs)
    return kept


def crop_figure_to_hotspots(
    png_path: Path,
    hotspots: list[FigureHotspot],
    *,
    padding: float = 0.08,
) -> bool:
    """Crop a rendered sheet to its exploded-view figure and remap hotspots.

    Callouts surround the illustrated assembly, so their collective extent is
    a safer crop signal than page ink (headers, footers, and parts tables are
    also ink). Sparse or clustered detections are left as a full page rather
    than risking a destructive crop.
    """
    if len(hotspots) < 2:
        return False
    x0 = max(0.0, min(hotspot.x0 for hotspot in hotspots) - padding)
    y0 = max(0.0, min(hotspot.y0 for hotspot in hotspots) - padding)
    x1 = min(1.0, max(hotspot.x1 for hotspot in hotspots) + padding)
    y1 = min(1.0, max(hotspot.y1 for hotspot in hotspots) + padding)
    if x1 - x0 < 0.25 or y1 - y0 < 0.25:
        return False
    try:
        from PIL import Image

        with Image.open(png_path) as image:
            width, height = image.size
            left = max(0, int(x0 * width))
            top = max(0, int(y0 * height))
            right = min(width, int(x1 * width + 0.999))
            bottom = min(height, int(y1 * height + 0.999))
            if right <= left or bottom <= top:
                return False
            cropped = image.crop((left, top, right, bottom))
            cropped.save(png_path, format="PNG")
    except (OSError, ValueError):
        return False

    actual_x0 = left / width
    actual_y0 = top / height
    actual_width = (right - left) / width
    actual_height = (bottom - top) / height
    for hotspot in hotspots:
        hotspot.x0 = round((hotspot.x0 - actual_x0) / actual_width, 4)
        hotspot.x1 = round((hotspot.x1 - actual_x0) / actual_width, 4)
        hotspot.y0 = round((hotspot.y0 - actual_y0) / actual_height, 4)
        hotspot.y1 = round((hotspot.y1 - actual_y0) / actual_height, 4)
    return True


def link_hotspots_to_parts(
    hotspots: list[FigureHotspot], index_to_part: dict[str, tuple[int, str]]
) -> None:
    """Attach the part (sequence, partNumber) each callout points at, in place.

    ``index_to_part`` maps an UPPER-cased figure index ("3", "5A") to the parts
    list entry it identifies. This is the callout -> part half of the link; the
    part -> callout box half is written back onto the entry by the caller.
    """
    for hs in hotspots:
        match = index_to_part.get(hs.index.upper())
        if match:
            hs.part_sequence, hs.part_number = match


_VISION_PROMPT = (
    "This image is one sheet of an illustrated parts breakdown (IPB) exploded-view "
    "drawing. Small INDEX CALLOUT numbers label each part; a callout may have a "
    "trailing letter (e.g. 5A) and a leader line to the part.\n"
    "Find ONLY these callout numbers on the drawing: {indices}.\n"
    "Return STRICT JSON and nothing else:\n"
    '{{"callouts":[{{"index":"14","box":[x0,y0,x1,y1]}}]}}\n'
    "box is the normalized bounding box (0..1, origin TOP-LEFT) tight around the "
    "printed callout NUMBER itself (not the part, not the leader line). Include a "
    "callout only if you can actually see that number. Omit any you cannot find."
)


def _downscale_png(png_path: Path, max_dim: int = 1568) -> bytes:
    """PNG bytes downscaled so the long edge <= max_dim (Claude's detail sweet
    spot) to bound payload/latency. Falls back to raw bytes on any failure."""
    try:
        from PIL import Image

        with Image.open(png_path) as im:
            w, h = im.size
            scale = min(1.0, max_dim / max(w, h))
            if scale < 1.0:
                resized = im.resize(
                    (int(w * scale), int(h * scale)),
                    Image.Resampling.LANCZOS,
                )
            else:
                resized = im
            import io

            buf = io.BytesIO()
            resized.convert("L").save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return png_path.read_bytes()


def vision_callouts(
    png_path: Path,
    target_indices: set[str],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> list[FigureHotspot]:
    """Locate callouts with a multimodal model (Sonnet 4.5 via the LiteLLM/Bedrock
    proxy) — the recall booster for callouts tesseract can't read on dense or
    scanned drawings. Boxes are approximate (padded to a usable target) and still
    validated against ``target_indices``. Best-effort: any failure returns []."""
    if not target_indices or not base_url or not api_key:
        return []
    import base64
    import json

    import httpx

    img_b64 = base64.b64encode(_downscale_png(png_path)).decode("ascii")
    prompt = _VISION_PROMPT.format(indices=", ".join(sorted(target_indices)))
    body = {
        "model": model,
        # Dense figures can carry 100+ callouts (~50 output tokens each), and
        # adaptive-thinking aliases spend budget before emitting JSON.
        "max_tokens": 8192,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            }
        ],
    }

    def one_call() -> dict | None:
        try:
            resp = httpx.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=body,
                headers={"authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            resp.raise_for_status()
            content = str(
                (((resp.json().get("choices") or [{}])[0]).get("message") or {}).get(
                    "content"
                )
                or ""
            )
        except Exception:
            return None
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None

    # One retry: a transient refusal or truncated JSON otherwise silently
    # costs every callout on the sheet.
    data = one_call()
    if not data or not data.get("callouts"):
        data = one_call()
    if not data:
        return []

    wanted = {t.strip().upper() for t in target_indices}
    out: list[FigureHotspot] = []
    for item in data.get("callouts") or []:
        if not isinstance(item, dict):
            continue
        idx = str(item.get("index") or "").strip().upper()
        box = item.get("box")
        if idx not in wanted or not isinstance(box, list) or len(box) != 4:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half_w = max((x1 - x0) / 2, _MIN_BOX_HALF)
        half_h = max((y1 - y0) / 2, _MIN_BOX_HALF)
        out.append(
            FigureHotspot(
                index=idx,
                x0=round(max(0.0, cx - half_w), 4),
                y0=round(max(0.0, cy - half_h), 4),
                x1=round(min(1.0, cx + half_w), 4),
                y1=round(min(1.0, cy + half_h), 4),
                confidence=50.0,  # model-sourced; below an OCR hit, above nothing
                detection_method="vision-model",
            )
        )
    return out


def _fig_key(value: str) -> str:
    return (value or "").strip().upper()


def _index_key(value: str) -> str:
    """Callout key for a parts-list index: composite "index/sheet" values
    ("1/1", "14/2") stamp only the index part on the drawing."""
    return _fig_key(value).split("/")[0].strip()


def attach_hotspots(  # noqa: C901 - per-figure render+detect+link+vision pipeline
    pdf_path: Path,
    entries: list[PartsListEntry],
    figures: list[FigureRecord],
    media_dir: Path,
    *,
    vision: dict | None = None,
) -> dict:
    """Render each figure sheet and wire the callout <-> part links, in place.

    For every figure: render its page to ``media/`` PNG, OCR the index callouts
    (validated against the parts the figure actually carries), then link both
    directions — each hotspot gets ``partSequence``/``partNumber`` (callout ->
    part), and each matched part entry gets ``callout_box`` + ``figure_media_key``
    (part -> callout). Returns ``{figures, rendered, hotspots, visionHotspots,
    linkedParts}``.

    Tesseract (pure pixels) is the precise-box baseline. When ``vision`` config is
    supplied (``{base_url, api_key, model, min_recall}``), figures where OCR found
    fewer than ``min_recall`` of their callouts get a Sonnet-4.5 vision pass for
    the MISSING callouts only — recall booster for dense / scanned drawings.
    """
    media_dir.mkdir(parents=True, exist_ok=True)

    # Group parts by their figure number; index -> (sequence, partNumber).
    # Composite "index/sheet" references additionally group per sheet, so a
    # multi-sheet figure only looks for the callouts printed on that sheet.
    by_fig: dict[str, dict[str, tuple[int, str]]] = {}
    by_fig_sheet: dict[tuple[str, str], dict[str, tuple[int, str]]] = {}
    entry_by_seq: dict[int, PartsListEntry] = {}
    for e in entries:
        if getattr(e, "row_type", "part") not in ("part", "kit", "end-item"):
            continue
        entry_fig = _fig_key(getattr(e, "figure_number_raw", ""))
        raw_idx = _fig_key(getattr(e, "figure_index_raw", ""))
        idx = _index_key(raw_idx)
        if not entry_fig or not idx:
            continue
        located = (e.sequence, getattr(e, "part_number_raw", ""))
        by_fig.setdefault(entry_fig, {}).setdefault(idx, located)
        if "/" in raw_idx:
            sheet = raw_idx.split("/", 1)[1].strip()
            if sheet:
                by_fig_sheet.setdefault((entry_fig, sheet), {}).setdefault(idx, located)
        entry_by_seq[e.sequence] = e

    vision = vision or {}
    min_recall = float(vision.get("min_recall", 0.75))
    max_vision_calls = int(vision.get("max_calls", 0) or 0)  # 0 = uncapped
    vision_calls = 0
    vision_ready = bool(
        vision.get("base_url") and vision.get("api_key") and vision.get("model")
    )

    # Figure-number reconciliation: some older TOs caption a figure "1-2" while
    # the parts list references it as "1". Allow a fallback from the caption's
    # leading number to the entry key, but ONLY when that leading number is
    # unique across figures (so it can't mis-associate two figures' parts).
    lead_counts: dict[str, int] = {}
    for f in figures:
        lead_counts[_fig_key(f.figure_number).split("-")[0]] = (
            lead_counts.get(_fig_key(f.figure_number).split("-")[0], 0) + 1
        )

    stats = {
        "figures": len(figures),
        "rendered": 0,
        "hotspots": 0,
        "visionCalls": 0,
        "visionHotspots": 0,
        "linkedParts": 0,
        "partsFigures": 0,
        "figuresWithHotspots": 0,
        "figuresMissingHotspots": 0,
        "secondChanceCalls": 0,
    }
    # Parts-joined figures that ended the primary pass empty get one
    # second-chance vision call (fallback model — typically the stronger
    # drawing-twin model) so a single flaky response can't leave a figure
    # permanently unlinked.
    second_chance: list[tuple[FigureRecord, Path, dict[str, tuple[int, str]]]] = []
    # Coverage is FIGURE-level (a multi-sheet figure is covered when any sheet
    # links) — continuation sheets legitimately carry no callouts.
    parts_fig_keys: set[str] = set()
    covered_fig_keys: set[str] = set()
    # Figures whose parts list uses explicit "index/sheet" references: sheets
    # absent from those references carry no callouts by declaration, so the
    # callout pass (and its vision spend) is skipped for them.
    figs_with_sheet_refs = {fig for (fig, _sheet) in by_fig_sheet}
    for fig in figures:
        if not fig.page_number:
            continue
        # Always capture the figure/drawing IMAGE — every illustration in the
        # document, not just parts figures (block diagrams, schematics, etc.).
        stem_name = re.sub(
            r"[^A-Za-z0-9._-]",
            "_",
            f"figure-{fig.figure_number}-{fig.sheet_number or '1'}",
        )
        png = render_figure_png(pdf_path, fig.page_number, media_dir / stem_name)
        if png is None:
            continue
        stats["rendered"] += 1
        fig.media_key = f"media/{png.name}"
        svg = render_figure_svg(pdf_path, fig.page_number, media_dir / stem_name)
        if svg is not None:
            fig.vector_key = f"media/{svg.name}"

        # Callout hotspots only when the figure joins to parts (an IPB exploded
        # view). Non-parts diagrams are still captured as images above.
        fig_key = _fig_key(fig.figure_number)
        sheet_key = _fig_key(fig.sheet_number or "1")
        index_to_part = by_fig_sheet.get((fig_key, sheet_key)) or by_fig.get(
            fig_key, {}
        )
        if (
            fig_key in figs_with_sheet_refs
            and (fig_key, sheet_key) not in by_fig_sheet
            and index_to_part
        ):
            # The parts list pins callouts to specific sheets and none point
            # here — a declared no-callout continuation sheet.
            parts_fig_keys.add(fig_key)
            continue
        if not index_to_part:
            lead = fig_key.split("-")[0]
            if lead in by_fig and lead_counts.get(lead, 0) == 1:
                index_to_part = by_fig[lead]
        if not index_to_part and len(by_fig) == 1 and len(figures) == 1:
            # One parts list, one illustration: they belong together even when
            # the caption and list disagree on the figure number (seen on
            # single-figure handbooks whose prose figure is numbered first).
            index_to_part = next(iter(by_fig.values()))
        if not index_to_part:
            continue
        parts_fig_keys.add(fig_key)

        all_indices = set(index_to_part.keys())
        hotspots = detect_figure_hotspots(png, all_indices)
        found = {h.index.upper() for h in hotspots}

        # Vision fallback for the callouts OCR missed, when recall is low and the
        # per-document vision-call budget is not yet exhausted.
        missing = {i for i in all_indices if i.upper() not in found}
        under_budget = max_vision_calls == 0 or vision_calls < max_vision_calls
        if (
            vision_ready
            and under_budget
            and missing
            and len(found) < min_recall * len(all_indices)
        ):
            vision_calls += 1
            stats["visionCalls"] += 1
            vis = vision_callouts(
                png,
                missing,
                base_url=vision["base_url"],
                api_key=vision["api_key"],
                model=vision["model"],
            )
            for hs in vis:
                if hs.index.upper() not in found:
                    hotspots.append(hs)
                    found.add(hs.index.upper())
                    stats["visionHotspots"] += 1

        link_hotspots_to_parts(hotspots, index_to_part)
        if crop_figure_to_hotspots(png, hotspots):
            # The vector export is still full-page; do not offer it as the
            # extracted figure until its viewBox can be cropped equivalently.
            fig.vector_key = ""
        fig.hotspots = [h.as_dict() for h in hotspots]
        stats["hotspots"] += len(hotspots)
        # Part -> callout: stamp the box + figure image back onto the entry.
        for hs in hotspots:
            if hs.part_sequence is not None and hs.part_sequence in entry_by_seq:
                entry = entry_by_seq[hs.part_sequence]
                entry.callout_box = (hs.x0, hs.y0, hs.x1, hs.y1)
                entry.figure_media_key = fig.media_key
                stats["linkedParts"] += 1
        if hotspots:
            covered_fig_keys.add(fig_key)
        else:
            second_chance.append((fig, png, index_to_part))

    fallback_model = vision.get("fallback_model") or vision.get("model")
    for fig, png, index_to_part in second_chance:
        if not (vision_ready and fallback_model):
            break
        fig_key = _fig_key(fig.figure_number)
        if fig_key in covered_fig_keys:
            # A sibling sheet already carries this figure's linked callouts —
            # empty continuation sheets are normal, don't spend a retry.
            continue
        stats["secondChanceCalls"] += 1
        stats["visionCalls"] += 1
        vis = vision_callouts(
            png,
            set(index_to_part.keys()),
            base_url=vision["base_url"],
            api_key=vision["api_key"],
            model=fallback_model,
        )
        link_hotspots_to_parts(vis, index_to_part)
        if crop_figure_to_hotspots(png, vis):
            fig.vector_key = ""
        fig.hotspots = [h.as_dict() for h in vis]
        stats["hotspots"] += len(vis)
        stats["visionHotspots"] += len(vis)
        for hs in vis:
            if hs.part_sequence is not None and hs.part_sequence in entry_by_seq:
                entry = entry_by_seq[hs.part_sequence]
                entry.callout_box = (hs.x0, hs.y0, hs.x1, hs.y1)
                entry.figure_media_key = fig.media_key
                stats["linkedParts"] += 1
        if vis:
            covered_fig_keys.add(fig_key)

    stats["partsFigures"] = len(parts_fig_keys)
    stats["figuresWithHotspots"] = len(parts_fig_keys & covered_fig_keys)
    stats["figuresMissingHotspots"] = len(parts_fig_keys - covered_fig_keys)
    return stats
