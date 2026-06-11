from __future__ import annotations

import json
from pathlib import Path

from docling_serve.powerpoint_courseware.artifact_writer import write_course_artifacts
from docling_serve.powerpoint_courseware.schema_validation import validate_artifact
from fixture_manifests import manifest_for_file


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "test_files"
OUT = ROOT / "tests" / "prototype" / "out" / "multi-format"


def test_course_model_writer_emits_valid_artifacts_for_all_target_file_types() -> None:
    files = [
        FIXTURES / "1220dd73-5621-458d-950e-657a6738fb14-updated AFTO Form 874 for presentation.pptx",
        FIXTURES / "generated-code-validation-procedures.docx",
        FIXTURES / "generated-training-workbook.xlsx",
        FIXTURES / "titan-authorized-user-acceptable-use-policy.pdf",
    ]

    manifests = {
        path.suffix.lower().lstrip("."): manifest_for_file(path)
        for path in files
        if path.suffix.lower() != ".pptx"
    }
    manifests["pptx"] = json.loads((ROOT / "tests" / "prototype" / "out" / "pptx-ooxml-geometry.json").read_text())

    for file_type, manifest in manifests.items():
        result = write_course_artifacts(
            manifest,
            OUT / file_type,
            source_manifest_key=f"{file_type}-manifest.json",
        )
        course_model = json.loads(result.course_model.read_text())
        summary = json.loads(result.course_analysis_summary.read_text())
        reengineering = json.loads(result.reengineering_input.read_text())

        validate_artifact(course_model, "course-model.schema.json")
        validate_artifact(summary, "course-analysis-summary.schema.json")
        validate_artifact(reengineering, "reengineering-input.schema.json")
        assert course_model["course"]["slides"]
        assert all(slide["role"] for slide in course_model["course"]["slides"])
        assert result.enriched_manifest.exists()


def test_xlsx_fixture_preserves_each_sheet_as_a_course_unit() -> None:
    manifest = manifest_for_file(FIXTURES / "generated-training-workbook.xlsx")

    assert [unit["title"] for unit in manifest["units"]] == ["Objectives", "Rubric", "Summary"]
    assert all(unit["unitType"] == "sheet" for unit in manifest["units"])


def test_standard_run_writes_multiformat_summary() -> None:
    summary = json.loads((ROOT / "tests" / "prototype" / "out" / "multi-format-summary.json").read_text())

    assert summary["status"] == "complete"
    assert set(summary["fileTypes"]) == {"pptx", "docx", "xlsx", "pdf"}
    assert summary["fileTypes"]["xlsx"]["unitCount"] == 3
    assert all(item["slideRecordCount"] > 0 for item in summary["fileTypes"].values())
    assert all(item["llmRequests"] == 0 for item in summary["fileTypes"].values())
