"""Extract the three production acceptance PDFs into local review bundles."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from docling_serve.technical_order.extract import extract_technical_order
from docling_serve.technical_order.mpl import FigureRecord
from docling_serve.technical_order.schematic_figures import (
    extract_schematic_figure_bundle,
)

DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "docs" / "tests"


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "document"


def figure_records(payload: dict) -> list[FigureRecord]:
    return [
        FigureRecord(
            figure_number=str(figure.get("figureNumber") or ""),
            figure_title=str(figure.get("figureTitle") or ""),
            sheet_number=str(figure.get("sheetNumber") or ""),
            sheet_total=figure.get("sheetTotal"),
            page_number=int(figure.get("pageNumber") or 0),
            media_key=str(figure.get("mediaKey") or ""),
            hotspots=list(figure.get("hotspots") or []),
        )
        for figure in payload.get("figures") or []
        if isinstance(figure, dict)
    ]


def verify(pdf_path: Path, output_root: Path) -> dict:
    bundle = output_root / safe_name(pdf_path.stem)
    shutil.rmtree(bundle, ignore_errors=True)
    media_dir = bundle / "media"
    payload = extract_technical_order(pdf_path, media_dir=media_dir)
    schematic = extract_schematic_figure_bundle(
        pdf_path,
        figure_records(payload),
        bundle / "technical-order-schematics",
        figure_only=int(payload.get("entryCount") or 0) == 0,
        max_pages=8,
    )
    if schematic:
        payload["schematicFigures"] = schematic
        payload["bom"]["schematicFigures"] = schematic
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bom.json").write_text(
        json.dumps(payload["bom"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    summary = {
        "file": pdf_path.name,
        "entryCount": payload["entryCount"],
        "figureCount": payload["figureCount"],
        "renderedFigures": sum(
            1 for figure in payload["figures"] if figure.get("mediaKey")
        ),
        "schematicComponents": (schematic or {}).get("componentCount", 0),
        "schematicNets": (schematic or {}).get("netCount", 0),
        "schematicPages": len((schematic or {}).get("sourcePages") or []),
        "bundle": str(bundle),
    }
    (bundle / "verification.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/production-extraction")
    )
    args = parser.parse_args()
    samples = sorted(args.input.glob("*.pdf"))
    if len(samples) != 3:
        raise SystemExit(f"expected 3 PDF samples, found {len(samples)}")
    summaries = [verify(sample, args.output) for sample in samples]
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
