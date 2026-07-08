"""Content typing for bundle artifacts at rest (see storage.content_type_for)."""

from pathlib import Path

import pytest

from docling_serve.storage import content_type_for


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("schematic/schematic.svg", "image/svg+xml"),
        ("media/page-001.png", "image/png"),
        ("extraction.json", "application/json"),
        ("document.md", "text/markdown; charset=utf-8"),
        ("document.html", "text/html; charset=utf-8"),
        ("source.pdf", "application/pdf"),
        ("main_schematic.kbl", "application/xml"),
        ("main_schematic.xml", "application/xml"),
        ("main_schematic.edml", "text/plain; charset=utf-8"),
        ("main_schematic.net", "text/plain; charset=utf-8"),
        ("main_schematic.cir", "text/plain; charset=utf-8"),
        ("schematic.kicad_sch", "text/plain; charset=utf-8"),
        ("main_schematic.eevision.csv", "text/csv; charset=utf-8"),
        ("main_schematic.edb", "application/octet-stream"),
        ("mystery.blob", "application/octet-stream"),
    ],
)
def test_content_type_for(name: str, expected: str) -> None:
    assert content_type_for(name) == expected
    assert content_type_for(Path(name)) == expected


def test_is_case_insensitive() -> None:
    assert content_type_for("PAGE-001.PNG") == "image/png"
    assert content_type_for("SCHEMATIC.SVG") == "image/svg+xml"
