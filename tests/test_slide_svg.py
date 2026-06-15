"""Slide SVG previews rendered from structured geometry (no LibreOffice rasterization)."""

from __future__ import annotations

import base64
from pathlib import Path

from docling_serve.deep_document.slide_svg import render_slide_svg, write_slide_svgs
from docling_serve.extraction.service import _is_slide_deck


def _unit(**overrides):
    unit = {
        "unitId": "slide-0001",
        "unitType": "slide",
        "slideNumber": 1,
        "render": {
            "size": {"px": {"width": 960, "height": 720}},
            "background": {"color": "#0B1021"},
        },
        "content": {
            "elements": [
                {
                    "elementId": "e1",
                    "type": "text",
                    "zIndex": 1,
                    "bbox": {"x": 60, "y": 80, "w": 840, "h": 120},
                    "text": {
                        "paragraphs": [
                            {
                                "text": "Hello World",
                                "sizePt": 40,
                                "runs": [
                                    {"text": "Hello World", "bold": True, "color": "#FFFFFF", "sizePt": 40}
                                ],
                            }
                        ]
                    },
                },
            ]
        },
    }
    unit.update(overrides)
    return unit


def test_render_slide_svg_from_geometry() -> None:
    svg = render_slide_svg(_unit(), media_dir=Path("/tmp/does-not-exist"))
    assert svg.startswith("<svg")
    assert 'viewBox="0 0 960 720"' in svg
    assert "#0B1021" in svg  # slide background
    assert "Hello World" in svg
    assert 'font-weight="700"' in svg  # bold title


def test_image_elements_embed_as_data_uri(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    # A 1x1 transparent PNG.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    (media / "abc123.png").write_bytes(png)
    unit = _unit(
        content={
            "elements": [
                {
                    "elementId": "img1",
                    "type": "image",
                    "zIndex": 1,
                    "bbox": {"x": 0, "y": 0, "w": 200, "h": 200},
                    "assetRef": "media/abc123.png",
                }
            ]
        }
    )
    svg = render_slide_svg(unit, media_dir=media)
    # Self-contained: the image is embedded so the SVG renders even when loaded as <img>.
    assert "data:image/png;base64," in svg


def test_write_slide_svgs_writes_one_per_slide(tmp_path: Path) -> None:
    media = tmp_path / "media"
    units = [_unit(unitId="slide-0001", slideNumber=1), _unit(unitId="slide-0002", slideNumber=2)]
    written = write_slide_svgs(units, media_dir=media)
    # Non-padded, 1-based page numbers — matches the bundle contract the
    # workbench deck surface reads (`media/slide-${page}.svg`).
    assert written == ["media/slide-1.svg", "media/slide-2.svg"]
    assert (media / "slide-1.svg").read_text().startswith("<svg")
    assert (media / "slide-2.svg").read_text().startswith("<svg")


def test_is_slide_deck_detects_geometry() -> None:
    assert _is_slide_deck({"document": {"unitType": "slide", "units": [_unit()]}}) is True
    assert _is_slide_deck({"document": {"unitType": "section", "units": [{"unitType": "section"}]}}) is False
