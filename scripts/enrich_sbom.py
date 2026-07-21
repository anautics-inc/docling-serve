"""Add pinned non-Python runtime components to a CycloneDX SBOM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MAVEN_COMPONENTS = (
    ("io.github.spannm", "jackcess", "5.1.4"),
    ("org.apache.poi", "poi", "5.5.1"),
    ("commons-codec", "commons-codec", "1.20.0"),
    ("org.apache.commons", "commons-collections4", "4.5.0"),
    ("org.apache.commons", "commons-math3", "3.6.1"),
    ("commons-io", "commons-io", "2.21.0"),
    ("com.zaxxer", "SparseBitSet", "1.3"),
    ("org.apache.logging.log4j", "log4j-api", "2.24.3"),
)


def enrich(payload: dict) -> dict:
    components = payload.setdefault("components", [])
    existing = {component.get("purl") for component in components}
    for group, name, version in MAVEN_COMPONENTS:
        purl = f"pkg:maven/{group}/{name}@{version}"
        if purl in existing:
            continue
        components.append(
            {
                "type": "library",
                "group": group,
                "name": name,
                "version": version,
                "purl": purl,
                "properties": [
                    {
                        "name": "docling-serve:runtime-purpose",
                        "value": "microsoft-access-fallback",
                    }
                ],
            }
        )
    components.sort(
        key=lambda component: (
            str(component.get("purl") or ""),
            str(component.get("name") or ""),
        )
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    args = parser.parse_args()
    payload = enrich(json.loads(args.sbom.read_text(encoding="utf-8")))
    args.sbom.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
