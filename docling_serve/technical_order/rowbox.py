"""Row bounding boxes for parts-list entries.

``pdftotext -bbox-layout`` reports line-level coordinates from the PDF text
layer. Each parsed entry is matched back to its printed line (greedy, in
reading order, keyed on the part-number token) and stamped with a normalized
``[x0, y0, x1, y1]`` box (fractions of page size). The viewer overlays that
box on the rendered page image — in-PDF highlight without a PDF viewer.

Only meaningful for text-layer sources (born-digital or vendor OCR layers);
tesseract-sourced parses have no PDF coordinates and are skipped.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from docling_serve.execution.subprocesses import ExternalCommandError, run_external

_XHTML_NS = "{http://www.w3.org/1999/xhtml}"
_WS = re.compile(r"\s+")
# Dirty vendor-OCR text layers leak C0 control characters into the words that
# ``pdftotext -bbox-layout`` emits, which are invalid XML 1.0 and abort the
# whole parse (losing every row box in the document). Strip them up front.
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(slots=True)
class LineBox:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(slots=True)
class PageLines:
    width: float
    height: float
    lines: list[LineBox]


def page_line_boxes(pdf_path: Path) -> dict[int, PageLines]:
    """Per-page text lines with PDF-space bounding boxes (1-based pages)."""
    out = run_external(
        ["pdftotext", "-bbox-layout", str(pdf_path), "-"],
        check=True,
        timeout=300,
    )
    xml_text = _XML_INVALID.sub("", out.stdout.decode("utf-8", errors="replace"))
    root = ET.fromstring(xml_text)
    pages: dict[int, PageLines] = {}
    for page_no, page in enumerate(root.iter(f"{_XHTML_NS}page"), start=1):
        width = float(page.get("width", "0") or 0)
        height = float(page.get("height", "0") or 0)
        lines: list[LineBox] = []
        for line in page.iter(f"{_XHTML_NS}line"):
            words = [w.text or "" for w in line.iter(f"{_XHTML_NS}word")]
            text = _WS.sub(" ", " ".join(words)).strip()
            if not text:
                continue
            lines.append(
                LineBox(
                    text=text,
                    x0=float(line.get("xMin", "0") or 0),
                    y0=float(line.get("yMin", "0") or 0),
                    x1=float(line.get("xMax", "0") or 0),
                    y1=float(line.get("yMax", "0") or 0),
                )
            )
        lines.sort(key=lambda lb: (lb.y0, lb.x0))
        pages[page_no] = PageLines(width=width, height=height, lines=lines)
    return pages


def _match_token(entry) -> str:
    """The most distinctive printed token for locating the entry's line."""
    part = (entry.part_number_raw or "").strip()
    if part and not re.match(r"^NO\s+NUMBER$", part, re.I):
        # Wrapped part numbers continue on the next line; the first chunk is
        # what appears on the entry's primary line.
        return part.split()[0]
    desc = re.sub(r"^[. ]+", "", entry.description_raw or "").strip()
    return desc.split()[0] if desc else ""


def attach_row_boxes(entries, pdf_path: Path) -> int:
    """Stamp ``row_box`` (normalized fractions) onto entries; returns matches.

    Greedy in-order matching per page: entries and printed lines both run in
    reading order, so repeated part numbers (NUT, WASHER ...) stay aligned
    with their own rows.
    """
    if not entries:
        return 0
    try:
        pages = page_line_boxes(pdf_path)
    except (ExternalCommandError, ET.ParseError):
        return 0

    matched = 0
    by_page: dict[int, list] = {}
    for entry in entries:
        by_page.setdefault(entry.page_number, []).append(entry)

    for page_no, page_entries in by_page.items():
        page = pages.get(page_no)
        if page is None or page.width <= 0 or page.height <= 0:
            continue
        cursor = 0
        for entry in sorted(page_entries, key=lambda e: e.sequence):
            token = _match_token(entry)
            if not token:
                continue
            for i in range(cursor, len(page.lines)):
                line = page.lines[i]
                if token in line.text.split():
                    entry.row_box = (
                        round(line.x0 / page.width, 4),
                        round(line.y0 / page.height, 4),
                        round(line.x1 / page.width, 4),
                        round(line.y1 / page.height, 4),
                    )
                    cursor = i + 1
                    matched += 1
                    break
    return matched
