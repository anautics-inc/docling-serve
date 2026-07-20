"""Hotspot coverage guarantee: second-chance vision pass + coverage stats."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from docling_serve.technical_order import figure_hotspots as fh
from docling_serve.technical_order.mpl import FigureRecord, PartsListEntry

_VISION = {
    "base_url": "http://proxy/v1",
    "api_key": "k",
    "model": "primary-model",
    "fallback_model": "fallback-model",
    "min_recall": 0.75,
    "max_calls": 10,
}


def _fixture():
    entries = [
        PartsListEntry(
            sequence=1,
            page_number=3,
            figure_number_raw="1-1",
            figure_index_raw="1",
            part_number_raw="21435",
        ),
        PartsListEntry(
            sequence=2,
            page_number=3,
            figure_number_raw="1-2",
            figure_index_raw="1",
            part_number_raw="21407",
        ),
    ]
    figures = [
        FigureRecord(figure_number="1-1", figure_title="Assembly", page_number=1),
        FigureRecord(figure_number="1-2", figure_title="Base", page_number=2),
    ]
    return entries, figures


def _patch_render(monkeypatch, tmp_path: Path):
    def fake_png(pdf_path, page_number, stem):
        p = tmp_path / f"{stem.name}.png"
        p.write_bytes(b"png")
        return p

    monkeypatch.setattr(fh, "render_figure_png", fake_png)
    monkeypatch.setattr(fh, "render_figure_svg", lambda *a, **k: None)
    monkeypatch.setattr(fh, "detect_figure_hotspots", lambda png, indices: [])


def test_second_chance_recovers_empty_figures(monkeypatch, tmp_path):
    entries, figures = _fixture()
    _patch_render(monkeypatch, tmp_path)
    calls = []

    def fake_vision(png, indices, *, base_url, api_key, model):
        calls.append((png.name, model))
        # Primary model flakes on figure 1-2; the fallback model recovers it.
        if "1-2" in png.name and model == "primary-model":
            return []
        return [
            fh.FigureHotspot(index=i, x0=0.1, y0=0.1, x1=0.2, y1=0.2, confidence=90.0)
            for i in sorted(indices)
        ]

    monkeypatch.setattr(fh, "vision_callouts", fake_vision)
    stats = fh.attach_hotspots(
        Path("doc.pdf"), entries, figures, tmp_path, vision=_VISION
    )

    assert stats["partsFigures"] == 2
    assert stats["figuresWithHotspots"] == 2
    assert stats["figuresMissingHotspots"] == 0
    assert stats["secondChanceCalls"] == 1
    assert ("figure-1-2-1.png", "fallback-model") in calls
    # Both figures now carry linked hotspots, and both parts point back.
    assert all(f.hotspots for f in figures)
    assert all(e.callout_box for e in entries)


def test_sheet_pinned_callouts_skip_continuation_sheets(monkeypatch, tmp_path):
    """When the parts list pins callouts to sheets ("index/sheet" refs), sheets
    with no pinned callouts are declared continuation sheets: no vision spend,
    and the FIGURE still counts as covered."""
    entries = [
        PartsListEntry(
            sequence=1,
            page_number=3,
            figure_number_raw="29",
            figure_index_raw="1/1",
            part_number_raw="21435",
        ),
        PartsListEntry(
            sequence=2,
            page_number=3,
            figure_number_raw="29",
            figure_index_raw="2/1",
            part_number_raw="21407",
        ),
    ]
    figures = [
        FigureRecord(
            figure_number="29", figure_title="Tubing", page_number=1, sheet_number="1"
        ),
        FigureRecord(
            figure_number="29", figure_title="Tubing", page_number=2, sheet_number="4"
        ),
    ]
    _patch_render(monkeypatch, tmp_path)
    calls = []

    def fake_vision(png, indices, *, base_url, api_key, model):
        calls.append(png.name)
        return [
            fh.FigureHotspot(index=i, x0=0.1, y0=0.1, x1=0.2, y1=0.2, confidence=90.0)
            for i in sorted(indices)
        ]

    monkeypatch.setattr(fh, "vision_callouts", fake_vision)
    stats = fh.attach_hotspots(
        Path("doc.pdf"), entries, figures, tmp_path, vision=_VISION
    )

    assert stats["partsFigures"] == 1  # figure-level, not per sheet
    assert stats["figuresWithHotspots"] == 1
    assert stats["figuresMissingHotspots"] == 0
    # Sheet 4 (no pinned callouts) never reached vision.
    assert all("29-4" not in name for name in calls)


def test_covered_figures_skip_second_chance_for_empty_sibling_sheets(
    monkeypatch, tmp_path
):
    """A multi-sheet figure without sheet-pinned refs: when one sheet links,
    an empty sibling sheet is not retried and the figure counts covered."""
    entries = [
        PartsListEntry(
            sequence=1,
            page_number=3,
            figure_number_raw="8-5",
            figure_index_raw="1",
            part_number_raw="21435",
        ),
    ]
    figures = [
        FigureRecord(
            figure_number="8-5", figure_title="Engine", page_number=1, sheet_number="1"
        ),
        FigureRecord(
            figure_number="8-5", figure_title="Engine", page_number=2, sheet_number="4"
        ),
    ]
    _patch_render(monkeypatch, tmp_path)
    models_called = []

    def fake_vision(png, indices, *, base_url, api_key, model):
        models_called.append((png.name, model))
        if "8-5-4" in png.name:
            return []
        return [
            fh.FigureHotspot(index=i, x0=0.1, y0=0.1, x1=0.2, y1=0.2, confidence=90.0)
            for i in sorted(indices)
        ]

    monkeypatch.setattr(fh, "vision_callouts", fake_vision)
    stats = fh.attach_hotspots(
        Path("doc.pdf"), entries, figures, tmp_path, vision=_VISION
    )

    assert stats["partsFigures"] == 1
    assert stats["figuresWithHotspots"] == 1
    assert stats["figuresMissingHotspots"] == 0
    assert stats["secondChanceCalls"] == 0
    assert ("figure-8-5-4.png", "fallback-model") not in models_called


def test_residual_gap_is_counted(monkeypatch, tmp_path):
    entries, figures = _fixture()
    _patch_render(monkeypatch, tmp_path)
    monkeypatch.setattr(
        fh,
        "vision_callouts",
        lambda png, indices, **kw: (
            []
            if "1-2" in png.name
            else [
                fh.FigureHotspot(
                    index=i, x0=0.1, y0=0.1, x1=0.2, y1=0.2, confidence=90.0
                )
                for i in sorted(indices)
            ]
        ),
    )
    stats = fh.attach_hotspots(
        Path("doc.pdf"), entries, figures, tmp_path, vision=_VISION
    )
    assert stats["partsFigures"] == 2
    assert stats["figuresWithHotspots"] == 1
    assert stats["figuresMissingHotspots"] == 1


def test_crop_figure_to_hotspots_removes_page_and_remaps_boxes(tmp_path):
    image_path = tmp_path / "sheet.png"
    Image.new("RGB", (1000, 1000), "white").save(image_path)
    hotspots = [
        fh.FigureHotspot(index="1", x0=0.2, y0=0.3, x1=0.24, y1=0.34, confidence=90),
        fh.FigureHotspot(index="2", x0=0.7, y0=0.75, x1=0.74, y1=0.79, confidence=90),
    ]

    assert fh.crop_figure_to_hotspots(image_path, hotspots, padding=0.05) is True

    with Image.open(image_path) as cropped:
        assert cropped.width < 700
        assert cropped.height < 700
    assert hotspots[0].x0 < 0.1
    assert hotspots[0].y0 < 0.1
    assert hotspots[1].x1 > 0.9
    assert hotspots[1].y1 > 0.9
