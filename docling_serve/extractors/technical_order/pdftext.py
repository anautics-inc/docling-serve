"""Layout-preserving page text for TO parsing.

``pdftotext -layout`` (poppler, already a runtime dependency via pdftocairo in
the schematic extractor) reconstructs the page as a fixed-width character grid
that keeps the MPL's column alignment — the property the deterministic parser
relies on.

For scanned documents the embedded text layer (if any) came from the scanning
vendor's OCR and varies wildly in quality, so ``ocr_page_texts`` provides a
fresh tesseract pass over rendered pages as an alternative text source. The
extractor parses both and keeps whichever yields the better parts list.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_OCR_RENDER_DPI = 300
_OCR_WORKERS = 4
# Vertical table rules OCR as pipe glyphs; they are never MPL content.
_RULE_GLYPHS = re.compile(r"\|")


def page_layout_texts(
    pdf_path: Path,
    *,
    first: int | None = None,
    last: int | None = None,
) -> list[str]:
    """Per-page layout-preserved text (1 string per page, form-feed split)."""
    cmd = ["pdftotext", "-layout", "-enc", "UTF-8"]
    if first is not None:
        cmd += ["-f", str(first)]
    if last is not None:
        cmd += ["-l", str(last)]
    cmd += [str(pdf_path), "-"]
    out = subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    text = out.stdout.decode("utf-8", errors="replace")
    pages = text.split("\f")
    if pages and pages[-1].strip() == "":
        pages.pop()
    return pages


def ocr_available() -> bool:
    """True when tesseract is on PATH (OCR fallback can run)."""
    return shutil.which("tesseract") is not None


def ocr_page_texts(pdf_path: Path, *, dpi: int = _OCR_RENDER_DPI) -> list[str]:
    """Per-page tesseract OCR text in a layout-preserving character grid.

    Pages render via ``pdftoppm`` at ``dpi`` and OCR with PSM 6 +
    ``preserve_interword_spaces`` so column gaps survive as space runs — the
    same shape ``pdftotext -layout`` produces, which keeps the downstream MPL
    column parser source-agnostic. Pipe glyphs (OCR'd table rules) are
    stripped to spaces.
    """
    with tempfile.TemporaryDirectory(prefix="to-ocr-") as td:
        tmp = Path(td)
        subprocess.run(
            ["pdftoppm", "-gray", "-r", str(dpi), str(pdf_path), str(tmp / "pg")],
            capture_output=True,
            check=True,
            timeout=1800,
        )
        images = sorted(tmp.glob("pg-*.pgm"))

        def ocr_one(image: Path) -> str:
            out = subprocess.run(
                [
                    "tesseract",
                    str(image),
                    "-",
                    "--psm",
                    "6",
                    "-c",
                    "preserve_interword_spaces=1",
                ],
                capture_output=True,
                timeout=300,
            )
            text = out.stdout.decode("utf-8", errors="replace")
            return _RULE_GLYPHS.sub(" ", text)

        with ThreadPoolExecutor(max_workers=_OCR_WORKERS) as pool:
            return list(pool.map(ocr_one, images))
