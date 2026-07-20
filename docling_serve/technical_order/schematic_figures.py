"""Extract schematic-like Technical Order figures into one editable graph."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pikepdf

from docling_serve.schematic.extract import extract_schematic
from docling_serve.technical_order.mpl import FigureRecord

SCHEMATIC_FIGURES_SCHEMA = "captify.technical-drawings.v1"

_SCHEMATIC_TITLE = re.compile(
    r"\bSCHEMATIC\b|\bWIRING\b|\bBLOCK\s+DIAGRAM\b|"
    r"\bSIGNAL\s+INTERFACES?\b|\bTEST\s+CONNECTIONS?\b",
    re.I,
)


def select_schematic_figures(
    figures: list[FigureRecord],
    *,
    figure_only: bool,
    max_pages: int,
) -> list[FigureRecord]:
    """Return unique source pages suitable for component/net extraction.

    Figure-only uploads are explicit drawing collections, so every page is a
    candidate. Full manuals stay conservative and require schematic vocabulary
    in the caption; exploded-view and ordinary parts illustrations remain in
    the BOM image workflow.
    """
    selected: list[FigureRecord] = []
    seen_pages: set[int] = set()
    for figure in figures:
        if figure.page_number <= 0 or figure.page_number in seen_pages:
            continue
        if not figure_only and not _SCHEMATIC_TITLE.search(figure.figure_title):
            continue
        selected.append(figure)
        seen_pages.add(figure.page_number)
        if len(selected) >= max_pages:
            break
    return selected


def extract_schematic_figure_bundle(
    pdf_path: Path,
    figures: list[FigureRecord],
    output_dir: Path,
    *,
    figure_only: bool,
    max_pages: int,
    runner: Callable[..., dict[str, Any]] = extract_schematic,
) -> dict[str, Any] | None:
    """Build one multi-sheet schematic bundle from selected TO figure pages."""
    selected = select_schematic_figures(
        figures,
        figure_only=figure_only,
        max_pages=max_pages,
    )
    if not selected:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="to-schematic-pages-") as temp_dir:
        subset_path = Path(temp_dir) / "schematic-figures.pdf"
        with pikepdf.Pdf.open(pdf_path) as source:
            subset = pikepdf.Pdf.new()
            for figure in selected:
                subset.pages.append(source.pages[figure.page_number - 1])
            subset.save(subset_path)
        result = runner(
            subset_path,
            output_dir,
            profile="technical-order-schematic",
        )

    graph = result.get("graph") or {}
    schematic_manifest = (result.get("manifest") or {}).get("schematic") or {}
    svg_paths = list(schematic_manifest.get("svg") or [])
    nested_root = "technical-order-schematics"
    source_pages = [
        {
            "figureNumber": figure.figure_number,
            "figureTitle": figure.figure_title,
            "sourcePage": figure.page_number,
            "schematicPage": index,
            "vector": (
                f"{nested_root}/{svg_paths[index - 1]}"
                if index <= len(svg_paths)
                else None
            ),
        }
        for index, figure in enumerate(selected, start=1)
    ]
    graph_path = str(
        schematic_manifest.get("graph") or "schematic/schematic-graph.json"
    )
    eevision_path = str(schematic_manifest.get("eevisionCsv") or "")
    return {
        "schema": SCHEMATIC_FIGURES_SCHEMA,
        "manifest": "technical-order-schematics/extraction.json",
        "graph": f"{nested_root}/{graph_path}",
        "eevision": f"{nested_root}/{eevision_path}" if eevision_path else None,
        "sourcePages": source_pages,
        "componentCount": len(graph.get("components") or []),
        "netCount": len(graph.get("nets") or []),
        "warnings": graph.get("warnings") or [],
    }
