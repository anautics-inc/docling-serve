"""Shared KiCad net injection and preview rendering."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from docling_serve.execution.subprocesses import run_external
from docling_serve.schematic.kicad_sch import (
    component_annotation_sexprs,
    inject_items,
    junction_sexprs,
    net_label_sexprs,
    net_wires_sexpr,
)
from docling_serve.schematic.kicad_symbols import (
    SymbolLibrary,
    build_symbol_instances,
    document_sheet_uuid,
    embed_lib_symbols,
    ensure_kicad_cli_config,
    find_symbol_dir,
)


def inject_net_wires(
    kicad_sch_paths: list[Path], graph: dict[str, Any], notes: list[str]
) -> None:
    """Write graph electrical and semantic elements into KiCad documents."""

    nets = graph.get("nets") or []
    components = graph.get("components") or []
    symbol_dir = find_symbol_dir()
    library = SymbolLibrary(symbol_dir) if symbol_dir else None
    total = {"wires": 0, "junctions": 0, "labels": 0, "annotations": 0, "symbols": 0}
    for page_index, kicad_path in enumerate(kicad_sch_paths, start=1):
        text = kicad_path.read_text()
        if "(wire (pts" in text:
            continue
        wires = net_wires_sexpr(nets, page_no=page_index)
        junctions = junction_sexprs(nets, page_no=page_index)
        labels = net_label_sexprs(nets, page_no=page_index)
        annotations = component_annotation_sexprs(components, page_no=page_index)
        items = wires + junctions + labels + annotations
        if library is not None:
            sheet_uuid = document_sheet_uuid(text)
            lib_defs, symbol_items, mapped = build_symbol_instances(
                graph,
                page_no=page_index,
                sheet_uuid=sheet_uuid,
                library=library,
            )
            items += symbol_items
            total["symbols"] += mapped
            if page_index == 1:
                from docling_serve.schematic.kicad_symbols import (
                    simulation_stimulus_items,
                )

                page_height = next(
                    (
                        float(page["height"])
                        for page in graph.get("pages") or []
                        if isinstance(page, dict) and page.get("height")
                    ),
                    792.0,
                )
                sim_defs, sim_items, sim_notes = simulation_stimulus_items(
                    graph,
                    library,
                    sheet_uuid=sheet_uuid,
                    page_height_pt=page_height,
                )
                lib_defs.update(sim_defs)
                items += sim_items
                notes.extend(sim_notes)
            text = embed_lib_symbols(text, lib_defs)
        if items:
            kicad_path.write_text(inject_items(text, items))
        total["wires"] += len(wires)
        total["junctions"] += len(junctions)
        total["labels"] += len(labels)
        total["annotations"] += len(annotations)
    if any(total.values()):
        notes.append(
            "kicad_elements: "
            + ", ".join(f"{count} {kind}" for kind, count in total.items() if count)
        )


def kicad_cli_available() -> bool:
    return shutil.which("kicad-cli") is not None


def export_pdf_svg(pdf_path: Path, page_number: int, target: Path):
    """Render one PDF page through the shared subprocess policy."""

    return run_external(
        [
            "pdftocairo",
            "-svg",
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            str(pdf_path),
            str(target),
        ],
        check=True,
        timeout=120,
    )


def export_kicad_svg(
    kicad_path: Path, output_dir: str | Path, *, no_background_color: bool = False
) -> subprocess.CompletedProcess[str]:
    """Export one schematic through the configured KiCad CLI."""
    ensure_kicad_cli_config()
    command = ["kicad-cli", "sch", "export", "svg"]
    if no_background_color:
        command.append("--no-background-color")
    return run_external(
        [*command, "--output", str(output_dir), str(kicad_path)],
        text=True,
        timeout=300,
    )


def render_kicad_previews(
    kicad_sch_paths: list[Path], schematic_dir: Path, *, notes: list[str]
) -> list[Path]:
    """Render generated KiCad schematics to SVG when kicad-cli is available."""
    if not kicad_sch_paths:
        return []
    if not kicad_cli_available():
        notes.append("kicad_render_unavailable: kicad-cli not installed")
        return []
    renders: list[Path] = []
    for index, sch_path in enumerate(kicad_sch_paths, start=1):
        try:
            with tempfile.TemporaryDirectory() as out_dir:
                result = export_kicad_svg(sch_path, out_dir, no_background_color=True)
                if result.returncode != 0:
                    notes.append(
                        f"kicad_render_failed page {index}: "
                        f"{result.stderr.strip()[:160]}"
                    )
                    continue
                produced = sorted(Path(out_dir).glob("*.svg"))
                if not produced:
                    notes.append(f"kicad_render_failed page {index}: no svg produced")
                    continue
                target = schematic_dir / f"kicad-render-page-{index:03d}.svg"
                target.write_bytes(produced[0].read_bytes())
                renders.append(target)
        except Exception as error:  # pragma: no cover - environment dependent
            notes.append(f"kicad_render_failed page {index}: {error}")
    return renders


# Deprecated private names retained for compatibility.
_inject_net_wires = inject_net_wires
_render_kicad_previews = render_kicad_previews

__all__ = [
    "inject_net_wires",
    "render_kicad_previews",
]
