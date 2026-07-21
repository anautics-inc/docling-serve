"""Validate that every shipped repository path has an explicit validation tier."""

from __future__ import annotations

import argparse
import fnmatch
import json
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _files(repository: Path, pattern: str) -> set[str]:
    return {
        path.relative_to(repository).as_posix()
        for path in repository.glob(pattern)
        if path.is_file() and "__pycache__" not in path.parts
    }


def repository_surfaces(repository: Path, ledger: dict[str, Any]) -> set[str]:
    surfaces = ledger["surfaces"]
    paths = _files(repository, str(surfaces["production_modules"]))
    paths.update(_files(repository, "docling_serve/**/*.pyi"))
    paths.update(_files(repository, str(surfaces["scripts"])))
    for pattern in surfaces["deployment"]:
        paths.update(_files(repository, str(pattern)))
    return paths


def classify_paths(paths: set[str], ledger: dict[str, Any]) -> dict[str, str]:
    default = str(ledger["default_tier"])
    classifications: dict[str, str] = {}
    for path in sorted(paths):
        tier = default
        for rule in ledger["classifications"]:
            if any(fnmatch.fnmatch(path, pattern) for pattern in rule["patterns"]):
                tier = str(rule["tier"])
                break
        classifications[path] = tier
    return classifications


def validate_ledger(repository: Path, ledger: dict[str, Any]) -> dict[str, str]:
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("path coverage ledger schema is stale")
    tiers = set(ledger.get("tiers", {}))
    default = ledger.get("default_tier")
    if default not in tiers:
        raise ValueError("path coverage default tier is undefined")
    for rule in ledger.get("classifications", []):
        if rule.get("tier") not in tiers or not rule.get("patterns"):
            raise ValueError("path coverage classification is invalid")
    for required in (
        ledger.get("required_route_test"),
        *ledger.get("required_entrypoint_tests", []),
    ):
        if not isinstance(required, str) or not (repository / required).is_file():
            raise ValueError(f"required path validation is missing: {required}")
    paths = repository_surfaces(repository, ledger)
    if not paths:
        raise ValueError("path coverage ledger discovered no repository surfaces")
    classified = classify_paths(paths, ledger)
    unknown = set(classified.values()) - tiers
    if unknown:
        raise ValueError(f"undefined path coverage tiers: {sorted(unknown)}")
    return classified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("architecture/path-coverage.json"),
    )
    args = parser.parse_args()
    repository = args.repository.resolve()
    ledger = json.loads((repository / args.ledger).read_text(encoding="utf-8"))
    classified = validate_ledger(repository, ledger)
    print(
        json.dumps(
            {
                "covered_paths": len(classified),
                "tiers": dict(sorted(Counter(classified.values()).items())),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
