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

import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Words below this tesseract confidence are drawing-geometry noise.
_MIN_CONFIDENCE = 60.0
#: Callout glyph height sanity band at 150 DPI (filters hairlines/titles).
_MIN_GLYPH_PX = 6
_MAX_GLYPH_PX = 48


@dataclass(slots=True)
class FigureHotspot:
    index: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "box": [self.x0, self.y0, self.x1, self.y1],
            "confidence": round(self.confidence, 1),
        }


def png_dimensions(png_path: Path) -> tuple[int, int]:
    """Width/height from the PNG IHDR header (no imaging dependency)."""
    with png_path.open("rb") as fh:
        header = fh.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {png_path}")
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def detect_figure_hotspots(
    png_path: Path, valid_indices: set[str]
) -> list[FigureHotspot]:
    """Locate index callouts on a rendered figure sheet.

    Only tokens that exist in the figure's parsed index set survive — the
    MPL is the source of truth for which callouts the figure carries.
    """
    if not valid_indices:
        return []
    try:
        width, height = png_dimensions(png_path)
    except (OSError, ValueError):
        return []
    if width <= 0 or height <= 0:
        return []

    try:
        out = subprocess.run(
            ["tesseract", str(png_path), "-", "--psm", "11", "tsv"],
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []

    wanted = {token.strip().upper() for token in valid_indices if token.strip()}
    hotspots: list[FigureHotspot] = []
    for line in out.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 12:
            continue
        text = fields[11].strip().upper().rstrip(".,")
        if text not in wanted:
            continue
        try:
            confidence = float(fields[10])
            x, y, w, h = (int(fields[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        if confidence < _MIN_CONFIDENCE:
            continue
        if not (_MIN_GLYPH_PX <= h <= _MAX_GLYPH_PX):
            continue
        hotspots.append(
            FigureHotspot(
                index=text,
                x0=round(x / width, 4),
                y0=round(y / height, 4),
                x1=round((x + w) / width, 4),
                y1=round((y + h) / height, 4),
                confidence=confidence,
            )
        )
    return hotspots
