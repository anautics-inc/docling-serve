"""Build Graphify evidence from an isolated working-tree snapshot."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts.architecture_evidence import (
        GRAPHIFY_VERSION,
        installed_graphify_version,
        materialize_snapshot,
        write_json,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from architecture_evidence import (  # type: ignore[no-redef]
        GRAPHIFY_VERSION,
        installed_graphify_version,
        materialize_snapshot,
        write_json,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("graphify-out/graph.json"))
    parser.add_argument(
        "--provenance",
        type=Path,
        default=Path("graphify-out/source-snapshot.json"),
    )
    args = parser.parse_args()
    if installed_graphify_version() != GRAPHIFY_VERSION:
        raise SystemExit(f"graphifyy=={GRAPHIFY_VERSION} is required")
    executable = shutil.which("graphify")
    if executable is None:
        raise SystemExit("graphify executable is not installed")

    repository = args.repository.resolve()
    with tempfile.TemporaryDirectory(prefix="docling-architecture-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir()
        provenance = materialize_snapshot(repository, snapshot)
        subprocess.run(
            [executable, "update", ".", "--no-cluster", "--force"],
            cwd=snapshot,
            check=True,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot / "graphify-out/graph.json", args.output)
    write_json(args.provenance, provenance)
    print(f"architecture source snapshot: {provenance['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
