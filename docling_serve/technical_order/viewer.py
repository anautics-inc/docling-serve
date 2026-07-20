"""Offline HTML viewer for a published technical-order bundle.

``build_viewer`` drops ``index.html`` + ``viewer-data.js`` next to ``bom.json``
so the whole bundle directory can be zipped and opened on a desktop with no
server: browsers block ``fetch()`` of ``file://`` JSON, so the bundle data is
embedded as a script global instead, while figure PNGs and schematic SVGs load
through relative ``<img>`` paths (which ``file://`` allows).

Run directly:  python -m docling_serve.technical_order.viewer BUNDLE_DIR [PDF]
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

_TEMPLATE = Path(__file__).with_name("viewer_template.html")


def build_viewer(bundle_dir: Path, source_pdf: Path | None = None) -> Path:
    """Write the viewer into ``bundle_dir``; returns the index.html path.

    When ``source_pdf`` is given it is copied into the bundle so the reviewer
    can open the original document alongside the extractions.
    """
    bom_path = bundle_dir / "bom.json"
    if not bom_path.is_file():
        raise FileNotFoundError(
            f"not a technical-order bundle (no bom.json): {bundle_dir}"
        )
    bom = json.loads(bom_path.read_text(encoding="utf-8"))

    data: dict = {
        "bom": bom,
        "schematicGraph": None,
        "schematicPages": [],
        "drawingTwin": None,
    }

    twin_rel = bom.get("drawingTwin")
    if isinstance(twin_rel, str) and (bundle_dir / twin_rel).is_file():
        data["drawingTwin"] = json.loads(
            (bundle_dir / twin_rel).read_text(encoding="utf-8")
        )

    schematic = bom.get("schematicFigures") or {}
    graph_rel = schematic.get("graph")
    if graph_rel and (bundle_dir / graph_rel).is_file():
        graph = json.loads((bundle_dir / graph_rel).read_text(encoding="utf-8"))
        data["schematicGraph"] = graph
        dims = {p.get("pageNumber"): p for p in graph.get("pages") or []}
        pages = []
        for src_page in schematic.get("sourcePages") or []:
            page = dict(src_page)
            info = dims.get(page.get("schematicPage")) or {}
            page["width"] = info.get("width")
            page["height"] = info.get("height")
            pages.append(page)
        data["schematicPages"] = pages

    if source_pdf is not None and source_pdf.is_file():
        dest = bundle_dir / "source" / source_pdf.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            shutil.copy2(source_pdf, dest)
        data["sourcePdf"] = f"source/{source_pdf.name}"

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # "</script" inside JSON string data would terminate the script tag early.
    payload = payload.replace("</", "<\\/")
    (bundle_dir / "viewer-data.js").write_text(
        f"window.CAPTIFY_BUNDLE = {payload};\n", encoding="utf-8"
    )
    index = bundle_dir / "index.html"
    shutil.copyfile(_TEMPLATE, index)
    return index


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: python -m docling_serve.technical_order.viewer BUNDLE_DIR [SOURCE_PDF]"
        )
    out = build_viewer(
        Path(sys.argv[1]),
        Path(sys.argv[2]) if len(sys.argv) > 2 else None,
    )
    print(out)
