from __future__ import annotations

import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pytest
import jsonschema

from docling_serve.powerpoint_courseware import build_course_artifacts
from docling_serve.powerpoint_courseware.pedagogy_provider import (
    BedrockConfigError,
    BedrockPedagogyProvider,
    DeterministicPedagogyProvider,
    provider_from_environment,
)
from docling_serve.powerpoint_courseware.schema_validation import validate_artifact


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "tests" / "prototype" / "out"


def _course_model() -> dict:
    return json.loads((OUT / "course-model.json").read_text())


def _geometry() -> dict:
    return json.loads((OUT / "pptx-ooxml-geometry.json").read_text())


def test_course_model_artifacts_are_emitted() -> None:
    course_model = _course_model()
    summary = json.loads((OUT / "course-analysis-summary.json").read_text())
    reengineering = json.loads((OUT / "reengineering-input.json").read_text())

    assert course_model["artifactKind"] == "course_model"
    assert summary["artifactKind"] == "course_analysis_summary"
    assert reengineering["artifactKind"] == "reengineering_input"
    assert course_model["providerUsage"]["llmRequests"] == 0
    assert summary["reengineeringCandidateCount"] == len(course_model["course"]["reengineeringCandidates"])


def test_course_model_artifacts_validate_against_published_schemas() -> None:
    validate_artifact(_course_model(), "course-model.schema.json")
    validate_artifact(
        json.loads((OUT / "course-analysis-summary.json").read_text()),
        "course-analysis-summary.schema.json",
    )
    validate_artifact(
        json.loads((OUT / "reengineering-input.json").read_text()),
        "reengineering-input.schema.json",
    )
    assert (OUT / "schemas" / "course-model.schema.json").exists()


def test_course_model_schema_rejects_unknown_slide_roles() -> None:
    course_model = _course_model()
    course_model["course"]["slides"][0]["role"] = ["MadeUpRole"]

    with pytest.raises(jsonschema.ValidationError):
        validate_artifact(course_model, "course-model.schema.json")


def test_objective_inference_is_not_fixture_hardcoded() -> None:
    manifest = {
        "source": {"sha256": "generic-fixture"},
        "units": [
            {
                "unitId": "unit-001",
                "unitType": "page",
                "index": 0,
                "title": "Generator Maintenance Checklist",
                "elements": [
                    {
                        "elementId": "unit-001-text-001",
                        "kind": "text",
                        "type": "text",
                        "text": "Technicians complete the startup checklist and verify safety approvals.",
                    }
                ],
            }
        ],
    }

    artifacts = build_course_artifacts(manifest, source_manifest_key="test-key")
    objective = artifacts["courseModel"]["course"]["objectives"][0]
    serialized = json.dumps(objective).lower()

    assert "afto" not in serialized
    assert "874" not in serialized
    assert "generator maintenance checklist" in objective["task"].lower()


def test_course_identity_uses_deck_title_not_later_repeated_slide_title() -> None:
    course = _course_model()["course"]

    assert course["metadata"]["courseTitle"] == "TIME COMPLIANCE TECHNICAL ORDER SUPPLY DATA REQUIREMENTS"
    assert course["modules"][0]["title"] == "TIME COMPLIANCE TECHNICAL ORDER SUPPLY DATA REQUIREMENTS"
    assert "part h - action required on supply records" not in course["objectives"][0]["task"].lower()


def test_explicit_objective_task_condition_standard_is_extracted() -> None:
    manifest = {
        "source": {"sha256": "objective-fixture"},
        "units": [
            {
                "unitId": "unit-001",
                "unitType": "page",
                "index": 0,
                "title": "Hydraulic Inspection Course",
                "elements": [
                    {
                        "elementId": "unit-001-text-001",
                        "kind": "text",
                        "type": "text",
                        "text": (
                            "Learning Objective: Perform hydraulic pressure inspection IAW TO "
                            "under field maintenance conditions with zero safety violations"
                        ),
                    }
                ],
            }
        ],
    }

    artifacts = build_course_artifacts(manifest, source_manifest_key="test-key")
    objective = artifacts["courseModel"]["course"]["objectives"][0]

    assert objective["type"] == "Terminal"
    assert objective["task"] == "perform hydraulic pressure inspection iaw to"
    assert objective["condition"] == "under field maintenance conditions"
    assert objective["standard"] == "with zero safety violations"
    assert objective["confidence"] == 1.0
    assert objective["source"] == "slide_text"


def test_positive_assessment_and_redundancy_fixtures_are_detected() -> None:
    manifest = {
        "source": {"sha256": "assessment-redundancy-fixture"},
        "units": [
            {
                "unitId": f"unit-{index:03d}",
                "unitType": "page",
                "index": index - 1,
                "title": "Repeated Safety Definition" if index <= 4 else "Knowledge Check",
                "elements": [
                    {
                        "elementId": f"unit-{index:03d}-text-001",
                        "kind": "text",
                        "type": "text",
                        "text": (
                            "Define the repeated safety term and explain the required procedure."
                            if index <= 4
                            else "Knowledge Check: Which action should the learner choose?"
                        ),
                    }
                ],
            }
            for index in range(1, 6)
        ],
    }

    artifacts = build_course_artifacts(manifest, source_manifest_key="test-key")
    course = artifacts["courseModel"]["course"]

    assert course["assessments"]
    assert course["assessments"][0]["slides"] == ["unit-005"]
    assert course["pedagogicalAnalysis"]["redundancy"]
    assert course["pedagogicalAnalysis"]["redundancy"][0]["slides"] == [
        "unit-001",
        "unit-002",
        "unit-003",
        "unit-004",
    ]


def test_course_model_has_one_pedagogical_record_per_slide() -> None:
    course_model = _course_model()
    slides = course_model["course"]["slides"]

    assert len(slides) == 27
    assert all(slide["slideId"].startswith("slide-") for slide in slides)
    assert all(slide["role"] for slide in slides)
    assert all(slide["primaryRole"] in slide["role"] for slide in slides)
    assert all(slide["provider"] == "deterministic" for slide in slides)


def test_classification_ignores_watermark_and_word_substrings() -> None:
    slides = {slide["slideId"]: slide for slide in _course_model()["course"]["slides"]}

    assert "Example" not in slides["slide-001"]["role"]
    assert slides["slide-001"]["containsPractice"] is False
    assert slides["slide-004"]["containsAssessment"] is False


def test_enriched_manifest_is_additive_and_source_manifest_is_not_mutated() -> None:
    geometry = _geometry()
    original = deepcopy(geometry)
    artifacts = build_course_artifacts(geometry, source_manifest_key="test-key")
    enriched = artifacts["enrichedManifest"]

    assert geometry == original
    assert "pedagogical" not in geometry["slides"][0]
    assert "pedagogical" in enriched["slides"][0]
    for before, after in zip(geometry["slides"], enriched["slides"], strict=True):
        stripped = dict(after)
        stripped.pop("pedagogical", None)
        assert stripped == before


def test_course_model_accepts_string_text_elements_from_normalized_manifests() -> None:
    manifest = {
        "source": {"sha256": "string-text-fixture"},
        "units": [
            {
                "unitId": "unit-001",
                "unitType": "page",
                "index": 0,
                "title": "String Text Fixture",
                "elements": [
                    {
                        "elementId": "unit-001-text-001",
                        "kind": "text",
                        "type": "text",
                        "text": "Learners complete AFTO Form 874 using the provided procedure.",
                    }
                ],
            }
        ],
    }

    artifacts = build_course_artifacts(manifest, source_manifest_key="test-key")

    course = artifacts["courseModel"]["course"]
    assert course["slides"][0]["slideId"] == "unit-001"
    assert course["slides"][0]["containsTaskInstruction"] is True


def test_modules_cover_all_slides_without_reordering() -> None:
    course_model = _course_model()
    modules = course_model["course"]["modules"]
    slide_ids = [slide["slideId"] for slide in course_model["course"]["slides"]]
    covered = [
        slide_id
        for module in modules
        for slide_id in module["slideIds"]
    ]

    assert len(modules) > 1
    assert covered == slide_ids
    assert any(module["title"] == "Part B - Action Required On Spares" for module in modules)
    assert any(
        module["title"] == "Part H - Action Required On Supply Records"
        and module["slideIds"] == ["slide-019", "slide-020", "slide-021"]
        for module in modules
    )


def test_objectives_include_air_force_task_condition_standard() -> None:
    objectives = _course_model()["course"]["objectives"]

    assert objectives
    objective = objectives[0]
    assert objective["type"] == "Inferred"
    assert objective["task"]
    assert objective["condition"]
    assert objective["standard"]
    assert objective["bloomLevel"] == "apply"


def test_every_objective_has_alignment_and_bloom_analysis() -> None:
    course = _course_model()["course"]
    objective_ids = {objective["id"] for objective in course["objectives"]}
    alignment_ids = {
        alignment["objectiveId"]
        for alignment in course["pedagogicalAnalysis"]["objectiveAlignment"]
    }
    bloom_ids = {
        bloom["objectiveId"]
        for bloom in course["pedagogicalAnalysis"]["bloomAnalysis"]
    }

    assert objective_ids
    assert objective_ids == alignment_ids == bloom_ids
    assert not any(
        bloom["mismatchType"] == "underBloomed"
        for bloom in course["pedagogicalAnalysis"]["bloomAnalysis"]
        if bloom["objectiveBloom"] == "apply"
    )


def test_every_module_has_gagne_sequence_and_density_covers_every_slide() -> None:
    course = _course_model()["course"]
    module_ids = {module["id"] for module in course["modules"]}
    sequence_ids = {
        sequence["moduleId"]
        for sequence in course["pedagogicalAnalysis"]["moduleSequences"]
    }
    slide_ids = {slide["slideId"] for slide in course["slides"]}
    density_ids = {
        density["slideId"]
        for density in course["pedagogicalAnalysis"]["slideDensity"]
    }

    assert module_ids == sequence_ids
    assert slide_ids == density_ids
    assert all(
        set(sequence["gagneCoverage"]) == {
            "gainAttention",
            "informObjectives",
            "recallPriorLearning",
            "presentContent",
            "guidance",
            "practice",
            "feedback",
            "assessment",
            "retentionTransfer",
        }
        for sequence in course["pedagogicalAnalysis"]["moduleSequences"]
    )


def test_assessments_are_linked_to_real_objectives_and_slides() -> None:
    course = _course_model()["course"]
    objective_ids = {objective["id"] for objective in course["objectives"]}
    slide_ids = {slide["slideId"] for slide in course["slides"]}

    for assessment in course["assessments"]:
        assert set(assessment["measuresObjective"]).issubset(objective_ids)
        assert set(assessment["slides"]).issubset(slide_ids)


def test_reengineering_input_contains_only_candidate_slides() -> None:
    course = _course_model()["course"]
    candidate_slide_ids = {
        slide_id
        for candidate in course["reengineeringCandidates"]
        for slide_id in candidate["slideIds"]
    }
    reengineering = json.loads((OUT / "reengineering-input.json").read_text())
    flagged_slide_ids = {slide["slideId"] for slide in reengineering["flaggedSlides"]}

    assert flagged_slide_ids
    assert flagged_slide_ids.issubset(candidate_slide_ids)
    assert all(slide["issues"] for slide in reengineering["flaggedSlides"])


def test_every_inference_object_carries_confidence_and_provider() -> None:
    course = _course_model()["course"]
    inferred_collections = [
        [course["metadata"]],
        course["modules"],
        course["slides"],
        course["objectives"],
        course["assessments"],
        course["pedagogicalAnalysis"]["objectiveAlignment"],
        course["pedagogicalAnalysis"]["bloomAnalysis"],
        course["pedagogicalAnalysis"]["moduleSequences"],
        course["pedagogicalAnalysis"]["slideDensity"],
        course["pedagogicalAnalysis"]["redundancy"],
        course["reengineeringCandidates"],
    ]

    for collection in inferred_collections:
        for item in collection:
            assert "confidence" in item
            assert "provider" in item


def test_provider_selection_is_deterministic_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCLING_SERVE_COURSE_MODEL_PROVIDER", raising=False)

    assert isinstance(provider_from_environment(), DeterministicPedagogyProvider)


def test_bedrock_provider_fails_closed_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCLING_SERVE_COURSE_MODEL_PROVIDER", "bedrock")
    monkeypatch.delenv("DOCLING_SERVE_COURSE_MODEL_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.setenv("DOCLING_SERVE_COURSE_MODEL_BEDROCK_REGION", "us-east-1")

    with pytest.raises(BedrockConfigError):
        provider_from_environment()


def test_bedrock_provider_uses_explicit_model_and_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCLING_SERVE_COURSE_MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("DOCLING_SERVE_COURSE_MODEL_BEDROCK_MODEL_ID", "model-id")
    monkeypatch.setenv("DOCLING_SERVE_COURSE_MODEL_BEDROCK_REGION", "us-east-1")

    provider = provider_from_environment()

    assert isinstance(provider, BedrockPedagogyProvider)
    assert provider.model_id == "model-id"


def test_build_course_artifacts_stamps_selected_provider() -> None:
    provider = BedrockPedagogyProvider(
        model_id="model-id",
        region="us-east-1",
        client=FakeBedrockClient(),
    )
    artifacts = build_course_artifacts(
        {
            "source": {"sha256": "provider-fixture"},
            "units": [
                {
                    "unitId": "unit-001",
                    "unitType": "page",
                    "index": 0,
                    "title": "Provider Fixture",
                    "elements": [
                        {
                            "elementId": "unit-001-text-001",
                            "kind": "text",
                            "type": "text",
                            "text": "Learners complete the procedure.",
                        }
                    ],
                }
            ],
        },
        source_manifest_key="test-key",
        provider=provider,
    )
    course_model = artifacts["courseModel"]

    assert course_model["providerUsage"]["provider"] == "aws_bedrock_structured_output"
    assert course_model["providerUsage"]["llmRequests"] == 2
    assert course_model["course"]["metadata"]["provider"] == "aws_bedrock_structured_output"
    assert course_model["course"]["slides"][0]["provider"] == "aws_bedrock_structured_output"
    assert course_model["course"]["instructionalDesignReview"]["recommendedStructure"]["instructionalModuleCount"] == 1
    assert course_model["course"]["slides"][0]["llmReview"]["recommendedInstructionalModule"] == "Provider Review Module"


class FakeBedrockClient:
    def invoke_model(self, **kwargs: object) -> dict[str, object]:
        response = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "courseReview": {
                                "recommendedStructure": {
                                    "sourceSectionCount": 1,
                                    "instructionalModuleCount": 1,
                                    "rationale": "One source section supports one performance module.",
                                    "instructionalModules": [
                                        {
                                            "id": "im-001",
                                            "title": "Provider Review Module",
                                            "sourceModuleIds": ["module-001"],
                                            "slideIds": ["unit-001"],
                                            "bloomLevel": "apply",
                                            "objective": "Complete the procedure.",
                                            "taskConditionStandard": {
                                                "task": "complete the procedure",
                                                "condition": "given source guidance",
                                                "standard": "accurately",
                                            },
                                            "rationale": "The content is procedural.",
                                        }
                                    ],
                                },
                                "bloomProgression": ["apply"],
                                "afIsdFindings": [],
                                "risks": [],
                                "recommendations": ["Add criterion-based practice."],
                                "confidence": 0.8,
                            },
                            "slideReviews": [
                                {
                                    "slideId": "unit-001",
                                    "instructionalRole": "Procedure",
                                    "bloomLevel": "apply",
                                    "sourceSection": "Provider Fixture",
                                    "recommendedInstructionalModule": "Provider Review Module",
                                    "taskConditionStandard": {
                                        "task": "complete the procedure",
                                        "condition": "given source guidance",
                                        "standard": "accurately",
                                    },
                                    "gaps": ["Needs practice"],
                                    "notesForAuthoring": ["Add a performance check."],
                                    "confidence": 0.82,
                                }
                            ],
                        }
                    ),
                }
            ],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }
        return {"body": BytesIO(json.dumps(response).encode("utf-8"))}


def test_preview_exposes_course_model_json_for_auditors() -> None:
    preview = (OUT / "preview.html").read_text()

    assert "Course Model JSON" in preview
    assert "Source Item JSON" in preview
    assert "course-model.json" in preview
    assert "&quot;slideId&quot;" in preview
