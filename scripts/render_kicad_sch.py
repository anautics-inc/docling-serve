"""Render a generated .kicad_sch to PNG and diff it against the source PDF.

Verification loop for the schematic extractor's KiCad geometry export
(:mod:`docling_serve.extractors.kicad_sch`): rasterise the ``.kicad_sch``
polylines exactly as written, then compare ink coverage against the original
PDF page so missing or spurious geometry is visible at a glance.

Usage:

    python scripts/render_kicad_sch.py bundle/schematic/schematic.kicad_sch \
        --pdf tests/test_files/main_schematic.pdf --out /tmp/schem-review

Outputs in ``--out``:

- ``render.png``      — the .kicad_sch geometry alone
- ``reference.png``   — the PDF page render (when ``--pdf`` is given)
- ``side_by_side.png``— reference above, render below
- ``overlay.png``     — black = in both, red = in PDF only (missing),
                        blue = in render only (extra)

and prints ink-coverage statistics (tolerant to 1px anti-aliasing offsets).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

#: KiCad schematic coordinates are millimetres.
MM_PER_INCH = 25.4

#: Pixels darker than this (0-255 gray) count as "ink" for the diff.
INK_THRESHOLD = 200

_XY_RE = re.compile(r"\(xy ([-\d.eE+]+) ([-\d.eE+]+)\)")
_WIDTH_RE = re.compile(r"\(width ([-\d.eE+]+)\)")
_COLOR_RE = re.compile(r"\(color (\d+) (\d+) (\d+)")
_PAPER_RE = re.compile(r'\(paper "User" ([-\d.eE+]+) ([-\d.eE+]+)\)')


def _parse_polylines(
    sch_text: str,
) -> list[tuple[list[tuple[float, float]], float, tuple[int, int, int], bool]]:
    """Extract ``(points_mm, width_mm, color, filled)`` from a .kicad_sch.

    Relies on the extractor's one-polyline-per-line serialization.
    """
    shapes = []
    for line in sch_text.splitlines():
        if not line.lstrip().startswith("(polyline"):
            continue
        points = [(float(x), float(y)) for x, y in _XY_RE.findall(line)]
        if len(points) < 2:
            continue
        width_match = _WIDTH_RE.search(line)
        width = float(width_match.group(1)) if width_match else 0.15
        color_match = _COLOR_RE.search(line)
        color = (
            (int(color_match.group(1)), int(color_match.group(2)), int(color_match.group(3)))
            if color_match
            else (0, 0, 0)
        )
        filled = "(fill (type outline))" in line
        shapes.append((points, width, color, filled))
    return shapes


def render_kicad_sch(sch_path: Path, *, dpi: int = 150) -> Image.Image:
    """Rasterise the polylines of a .kicad_sch (2x supersampled)."""
    text = sch_path.read_text()
    paper = _PAPER_RE.search(text)
    if not paper:
        raise SystemExit(f'{sch_path}: no (paper "User" W H) header found')
    width_mm, height_mm = float(paper.group(1)), float(paper.group(2))

    scale = dpi / MM_PER_INCH * 2  # supersample 2x, downsample at the end
    size = (round(width_mm * scale), round(height_mm * scale))
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)

    for points, width_mm_stroke, color, filled in _parse_polylines(text):
        pixels = [(x * scale, y * scale) for x, y in points]
        if filled and len(pixels) >= 3:
            draw.polygon(pixels, fill=color)
        else:
            stroke_px = max(1, round(width_mm_stroke * scale))
            draw.line(pixels, fill=color, width=stroke_px, joint="curve")

    return image.resize((size[0] // 2, size[1] // 2), Image.LANCZOS)


def render_pdf_page(pdf_path: Path, page_number: int, *, dpi: int = 150) -> Image.Image:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_number - 1]
        return page.render(scale=dpi / 72.0).to_pil().convert("RGB")
    finally:
        pdf.close()


def _ink_mask(image: Image.Image) -> Image.Image:
    """Binary (mode "1") mask of inked pixels."""
    return image.convert("L").point(lambda v: 255 if v < INK_THRESHOLD else 0)


def diff_images(
    reference: Image.Image, render: Image.Image
) -> tuple[Image.Image, dict[str, float]]:
    """Overlay + coverage stats; both images must share dimensions."""
    if render.size != reference.size:
        render = render.resize(reference.size, Image.LANCZOS)

    ref_mask = _ink_mask(reference)
    ren_mask = _ink_mask(render)
    # Tolerate 1px offsets from anti-aliasing / stroke-width rounding.
    ref_dilated = ref_mask.filter(ImageFilter.MaxFilter(3))
    ren_dilated = ren_mask.filter(ImageFilter.MaxFilter(3))

    # Histograms of mode-L masks: index 255 holds the ink count.
    ref_count = ref_mask.histogram()[255]
    ren_count = ren_mask.histogram()[255]

    from PIL import ImageChops

    missing = ImageChops.subtract(ref_mask, ren_dilated)  # in PDF, not in render
    extra = ImageChops.subtract(ren_mask, ref_dilated)  # in render, not in PDF
    both = ImageChops.subtract(ref_mask, missing)

    missing_count = missing.histogram()[255]
    extra_count = extra.histogram()[255]

    overlay = Image.new("RGB", reference.size, "white")
    overlay.paste((0, 0, 0), mask=both)
    overlay.paste((220, 30, 30), mask=missing)
    overlay.paste((40, 80, 220), mask=extra)

    stats = {
        "reference_ink_px": float(ref_count),
        "render_ink_px": float(ren_count),
        "missing_px": float(missing_count),
        "extra_px": float(extra_count),
        "coverage": 1.0 - (missing_count / ref_count) if ref_count else 1.0,
    }
    return overlay, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("kicad_sch", type=Path, help="Path to the .kicad_sch file")
    parser.add_argument("--pdf", type=Path, help="Source PDF to diff against")
    parser.add_argument("--page", type=int, default=1, help="PDF page number (1-based)")
    parser.add_argument("--dpi", type=int, default=150, help="Raster resolution")
    parser.add_argument(
        "--out", type=Path, default=Path("kicad-sch-review"), help="Output directory"
    )
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    render = render_kicad_sch(args.kicad_sch, dpi=args.dpi)
    render.save(args.out / "render.png")
    print(f"render.png          {render.size[0]}x{render.size[1]}")

    if not args.pdf:
        return 0

    reference = render_pdf_page(args.pdf, args.page, dpi=args.dpi)
    reference.save(args.out / "reference.png")

    overlay, stats = diff_images(reference, render)
    overlay.save(args.out / "overlay.png")

    gap = 10
    side = Image.new(
        "RGB", (reference.size[0], reference.size[1] * 2 + gap), "white"
    )
    side.paste(reference, (0, 0))
    side.paste(render.resize(reference.size), (0, reference.size[1] + gap))
    side.save(args.out / "side_by_side.png")

    print(f"reference.png       {reference.size[0]}x{reference.size[1]}")
    print("overlay.png         black=both  red=missing  blue=extra")
    print(
        f"ink coverage        {stats['coverage']:.2%} "
        f"(reference {stats['reference_ink_px']:.0f}px, "
        f"missing {stats['missing_px']:.0f}px, extra {stats['extra_px']:.0f}px)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
