"""Ad-hoc runner: extract one schematic PDF to a bundle dir for inspection.

Usage:
    python scripts/run_schematic_once.py <source.pdf> <out_bundle_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

from docling_serve.extractors.base import ExtractionContext
from docling_serve.extractors.schematic_extractor import SchematicExtractor


def main() -> int:
    source = Path(sys.argv[1]).resolve()
    bundle = Path(sys.argv[2]).resolve()
    bundle.mkdir(parents=True, exist_ok=True)

    def progress(stage, detail):
        print(f"  [stage] {stage} {detail or ''}", flush=True)

    ctx = ExtractionContext(
        source_path=source,
        bundle_dir=bundle,
        media_dir=bundle / "media",
        source_manifest_key=f"tenants/anautics/uploads/{source.name}",
        task_id="adhoc",
        profile="schematic",
        progress=progress,
    )
    result = SchematicExtractor().build(ctx)
    print("extractor:", result.extractor)
    print("componentCount:", result.structured.get("schematic", {}).get("componentCount"))
    print("netCount:", result.structured.get("schematic", {}).get("netCount"))
    print("graph:", (bundle / "schematic" / "schematic-graph.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
