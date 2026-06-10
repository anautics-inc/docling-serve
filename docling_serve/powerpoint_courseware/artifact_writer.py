from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from docling_serve.powerpoint_courseware import build_course_artifacts
from docling_serve.powerpoint_courseware.schema_validation import validate_artifact


@dataclass(frozen=True)
class CourseArtifactPaths:
    output_dir: Path
    course_model: Path
    course_analysis_summary: Path
    reengineering_input: Path
    enriched_manifest: Path
    schemas_dir: Path


def write_course_artifacts(
    manifest: dict[str, Any],
    output_dir: Path,
    *,
    source_manifest_key: str,
) -> CourseArtifactPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = build_course_artifacts(
        manifest,
        source_manifest_key=source_manifest_key,
    )
    paths = CourseArtifactPaths(
        output_dir=output_dir,
        course_model=output_dir / "course-model.json",
        course_analysis_summary=output_dir / "course-analysis-summary.json",
        reengineering_input=output_dir / "reengineering-input.json",
        enriched_manifest=output_dir / "enriched-manifest.json",
        schemas_dir=output_dir / "schemas",
    )
    validate_artifact(artifacts["courseModel"], "course-model.schema.json")
    validate_artifact(
        artifacts["analysisSummary"], "course-analysis-summary.schema.json"
    )
    validate_artifact(
        artifacts["reengineeringInput"], "reengineering-input.schema.json"
    )
    write_json(paths.course_model, artifacts["courseModel"])
    write_json(paths.course_analysis_summary, artifacts["analysisSummary"])
    write_json(paths.reengineering_input, artifacts["reengineeringInput"])
    write_json(paths.enriched_manifest, artifacts["enrichedManifest"])
    copy_schemas(paths.schemas_dir)
    return paths


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def copy_schemas(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for schema_file in resources.files(
        "docling_serve.powerpoint_courseware.schemas"
    ).iterdir():
        if schema_file.name.endswith(".json"):
            with resources.as_file(schema_file) as source:
                shutil.copyfile(source, target / schema_file.name)
