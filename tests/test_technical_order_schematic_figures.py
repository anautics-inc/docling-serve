from pathlib import Path

import pikepdf

from docling_serve.technical_order.mpl import FigureRecord
from docling_serve.technical_order.schematic_figures import (
    extract_schematic_figure_bundle,
    select_schematic_figures,
)


def test_full_manual_selects_only_schematic_caption_pages():
    figures = [
        FigureRecord("3-1", "TCU Signal Interfaces", page_number=16),
        FigureRecord("3-2", "Power Supply - Simplified Block Diagram", page_number=17),
        FigureRecord("7-1", "Control Unit - Exploded View", page_number=37),
    ]

    selected = select_schematic_figures(figures, figure_only=False, max_pages=8)

    assert [figure.page_number for figure in selected] == [16, 17]


def test_figure_only_bundle_preserves_source_page_mapping(tmp_path):
    source = (
        Path(__file__).resolve().parents[1] / "docs" / "tests" / "Test 1 figures.pdf"
    )
    figures = [FigureRecord(f"3-{page}", page_number=page) for page in range(1, 6)]

    def fake_runner(subset_path, output_dir, *, profile):
        with pikepdf.Pdf.open(subset_path) as subset:
            assert len(subset.pages) == 5
        assert profile == "technical-order-schematic"
        return {
            "manifest": {
                "schematic": {
                    "graph": "schematic/schematic-graph.json",
                    "svg": [
                        "schematic/schematic-page-001.svg",
                        "schematic/schematic-page-002.svg",
                        "schematic/schematic-page-003.svg",
                        "schematic/schematic-page-004.svg",
                        "schematic/schematic-page-005.svg",
                    ],
                    "eevisionCsv": "schematic/schematic-figures.eevision.csv",
                }
            },
            "graph": {
                "components": [{"id": "A1"}],
                "nets": [{"id": "N1"}],
                "warnings": ["review page 3"],
            },
        }

    result = extract_schematic_figure_bundle(
        source,
        figures,
        tmp_path / "technical-order-schematics",
        figure_only=True,
        max_pages=8,
        runner=fake_runner,
    )

    assert result is not None
    assert result["componentCount"] == 1
    assert result["netCount"] == 1
    assert [page["sourcePage"] for page in result["sourcePages"]] == [1, 2, 3, 4, 5]
    assert [page["schematicPage"] for page in result["sourcePages"]] == [1, 2, 3, 4, 5]
    assert result["sourcePages"][0]["vector"].endswith("schematic-page-001.svg")
    assert result["eevision"].endswith("schematic-figures.eevision.csv")


def test_single_page_bundle_uses_actual_unsuffixed_svg_key(tmp_path):
    source = (
        Path(__file__).resolve().parents[1] / "docs" / "tests" / "Test 1 figures.pdf"
    )

    def fake_runner(subset_path, output_dir, *, profile):
        return {
            "manifest": {
                "schematic": {
                    "graph": "schematic/schematic-graph.json",
                    "svg": ["schematic/schematic.svg"],
                }
            },
            "graph": {"components": [], "nets": []},
        }

    result = extract_schematic_figure_bundle(
        source,
        [FigureRecord("3-1", "Block Diagram", page_number=1)],
        tmp_path / "technical-order-schematics",
        figure_only=False,
        max_pages=8,
        runner=fake_runner,
    )

    assert result is not None
    assert result["sourcePages"][0]["vector"] == (
        "technical-order-schematics/schematic/schematic.svg"
    )
