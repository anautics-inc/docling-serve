"""Fill handling in the SVG -> .kicad_sch geometry replay.

KiCad paints polyline "outline" fills with the stroke color, so only
glyph/marker-scale paths may replay as filled polygons; larger filled paths
(component tints, title panels, border rings) must demote to their outline or
they flood the sheet and bury the line work.
"""

from docling_serve.schematic.kicad_sch import svg_to_kicad_sch


def _svg(body: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1224pt" height="792pt" '
        'viewBox="0 0 1224 792" version="1.2">' + body + "</svg>"
    )


def test_small_filled_glyph_keeps_outline_fill():
    sch = svg_to_kicad_sch(_svg('<path fill="black" d="M 10 10 L 18 10 L 18 18 Z"/>'))
    assert sch.count("(fill (type outline))") == 1


def test_large_filled_panel_demotes_to_outline():
    # A 300x200pt filled rectangle (e.g. a component body tint or title panel)
    # would flood its area if replayed filled.
    sch = svg_to_kicad_sch(
        _svg('<path fill="black" d="M 100 100 L 400 100 L 400 300 L 100 300 Z"/>')
    )
    assert "(fill (type outline))" not in sch
    assert sch.count("(polyline") == 1


def test_border_ring_subpaths_demote():
    # Border frames arrive as one path with outer+inner rectangle subpaths
    # (even-odd hole). Each subpath spans most of the page; both must demote.
    ring = (
        "M 20 20 L 1204 20 L 1204 772 L 20 772 Z "
        "M 30 30 L 1194 30 L 1194 762 L 30 762 Z"
    )
    sch = svg_to_kicad_sch(_svg(f'<path fill="black" d="{ring}"/>'))
    assert "(fill (type outline))" not in sch
    assert sch.count("(polyline") == 2


def test_page_background_still_dropped_entirely():
    sch = svg_to_kicad_sch(
        _svg('<path fill="white" d="M 0 0 L 1224 0 L 1224 792 L 0 792 Z"/>')
    )
    assert "(polyline" not in sch
