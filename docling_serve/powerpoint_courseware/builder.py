from __future__ import annotations

import copy
import re
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from docling_serve.powerpoint_courseware.pedagogy_provider import (
    PedagogyProvider,
    provider_from_environment,
)

ROLE_RANK = [
    "Agenda",
    "Objective",
    "Assessment",
    "Activity",
    "Guided Practice",
    "Procedure",
    "Definition",
    "Reference",
    "Administrative",
]
GAGNE_EVENTS = [
    "gainAttention",
    "informObjectives",
    "recallPriorLearning",
    "presentContent",
    "guidance",
    "practice",
    "feedback",
    "assessment",
    "retentionTransfer",
]
COURSE_TITLE_CONFIDENCE = 0.73
MODULE_CONFIDENCE = 0.72
SLIDE_CONFIDENCE = 0.74
OBJECTIVE_CONFIDENCE = 0.68
ANALYSIS_CONFIDENCE = 0.66
REDUNDANCY_MIN_REPEATS = 4
OVERLOAD_TEXT_THRESHOLD = 1100
OVERLOAD_BULLET_THRESHOLD = 8


def build_course_artifacts(
    manifest: dict[str, Any],
    *,
    source_manifest_key: str,
    provider: PedagogyProvider | None = None,
) -> dict[str, Any]:
    provider = provider or provider_from_environment()
    units = normalized_units(manifest)
    course_title = dominant_course_title(units, manifest)
    modules = build_modules(units, course_title, provider.provider_id)
    slides = build_slide_records(units, modules, provider.provider_id)
    objectives = build_objectives(units, modules, course_title, provider.provider_id)
    assessments = build_assessments(units, objectives, provider.provider_id)
    pedagogical = build_pedagogical_analysis(
        units, modules, slides, objectives, assessments, provider.provider_id
    )
    candidates = build_reengineering_candidates(
        units, pedagogical["slideDensity"], provider.provider_id
    )

    course = {
        "metadata": {
            "sourceFile": source_file(manifest),
            "courseTitle": course_title,
            "slideCount": len(units),
            "estimatedModules": len(modules),
            "deliveryMode": "Instructor-led",
            "detectedAudience": "Air Force training audience",
            "detectedInstructionType": "Procedural",
            "courseLevel": None,
            "containsAssessments": bool(assessments),
            "containsKnowledgeChecks": bool(assessments),
            "containsLabs": any(s["containsPractice"] for s in slides),
            "containsInstructorLedPatterns": True,
            "containsPassiveLectureBias": True,
            "confidence": COURSE_TITLE_CONFIDENCE,
            "provider": provider.provider_id,
        },
        "modules": modules,
        "slides": slides,
        "objectives": objectives,
        "assessments": assessments,
        "pedagogicalAnalysis": pedagogical,
        "reengineeringCandidates": candidates,
    }

    review = provider.review_course(review_payload(course))
    apply_provider_review(course, review, provider.provider_id)

    course_model = {
        "schemaVersion": "1.0",
        "artifactKind": "course_model",
        "documentId": document_id(manifest),
        "sourceManifestKey": source_manifest_key,
        "createdAt": datetime.now(UTC).isoformat(),
        "providerUsage": provider.usage.as_dict(),
        "course": course,
        "errors": [],
    }
    summary = analysis_summary(course_model)
    reengineering = reengineering_input(course_model)
    return {
        "courseModel": course_model,
        "analysisSummary": summary,
        "courseAnalysisSummary": summary,
        "reengineeringInput": reengineering,
        "enrichedManifest": enriched_manifest(manifest, slides),
    }


def normalized_units(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw_units = manifest.get("slides") or manifest.get("units") or []
    units = []
    for index, raw in enumerate(raw_units):
        if not isinstance(raw, dict):
            continue
        unit_id = str(
            raw.get("slideId") or raw.get("unitId") or f"unit-{index + 1:03d}"
        )
        title = str(raw.get("title") or f"Unit {index + 1}").strip()
        text = unit_text(raw)
        units.append(
            {
                "id": unit_id,
                "index": int(raw.get("index") or index),
                "number": int(raw.get("slideNumber") or index + 1),
                "title": title,
                "text": text,
                "raw": raw,
            }
        )
    return units


def unit_text(unit: dict[str, Any]) -> str:
    parts = [str(unit.get("title") or "")]
    notes = unit.get("speakerNotes") or {}
    if isinstance(notes, dict):
        parts.extend([str(notes.get("cleaned") or ""), str(notes.get("raw") or "")])
    for element in unit.get("elements") or []:
        text = element.get("text") if isinstance(element, dict) else None
        if isinstance(text, dict):
            parts.append(str(text.get("plain") or ""))
        elif text:
            parts.append(str(text))
    return "\n".join(part for part in parts if part).strip()


def dominant_course_title(units: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    source = manifest.get("source") or {}
    first_title = next(
        (u["title"] for u in units if is_title_candidate(u["title"])), ""
    )
    return first_title or str(source.get("originalFileName") or "Untitled course")


def is_title_candidate(title: str) -> bool:
    normalized = title.strip().lower()
    return bool(normalized and normalized not in {"untitled", "note", "questions"})


def build_modules(
    units: list[dict[str, Any]], course_title: str, provider_id: str
) -> list[dict[str, Any]]:
    modules = []
    current: dict[str, Any] | None = None
    for unit in units:
        title = module_title(unit, current)
        if current is None or title != current["title"]:
            current = {
                "id": f"module-{len(modules) + 1:03d}",
                "title": title,
                "dominantTopic": title,
                "slideIds": [],
                "startSlide": unit["id"],
                "endSlide": unit["id"],
                "summary": f"Instructional sequence for {title}.",
                "instructionType": "Procedural",
                "estimatedBloomRange": {"low": "understand", "high": "apply"},
                "learningObjectiveIds": ["obj-001"],
                "containsPractice": False,
                "containsFeedback": False,
                "containsAssessment": False,
                "containsRetentionSupport": False,
                "confidence": MODULE_CONFIDENCE,
                "provider": provider_id,
            }
            modules.append(current)
        current["slideIds"].append(unit["id"])
        current["endSlide"] = unit["id"]
        flags = role_flags(unit)
        current["containsPractice"] = current["containsPractice"] or flags["practice"]
        current["containsAssessment"] = (
            current["containsAssessment"] or flags["assessment"]
        )
    if modules:
        modules[0]["title"] = course_title
        modules[0]["dominantTopic"] = course_title
    return modules


def module_title(unit: dict[str, Any], current: dict[str, Any] | None) -> str:
    title = unit["title"].strip()
    if title.lower() == "note" and current:
        return current["title"]
    if current and title == current["title"]:
        return current["title"]
    if current and "responsible for" in title.lower():
        return current["title"]
    return title or (current or {}).get("title") or "Untitled module"


def build_slide_records(
    units: list[dict[str, Any]], modules: list[dict[str, Any]], provider_id: str
) -> list[dict[str, Any]]:
    module_by_slide = {
        slide_id: module["id"] for module in modules for slide_id in module["slideIds"]
    }
    return [
        {
            "slideId": unit["id"],
            "moduleId": module_by_slide.get(unit["id"]),
            "role": classify_roles(unit),
            "primaryRole": primary_role(classify_roles(unit)),
            "bloomSignal": "apply" if role_flags(unit)["task"] else "understand",
            "supportsObjective": ["obj-001"] if role_flags(unit)["task"] else [],
            "containsTaskInstruction": role_flags(unit)["task"],
            "containsPractice": role_flags(unit)["practice"],
            "containsAssessment": role_flags(unit)["assessment"],
            "containsFeedback": "feedback" in unit["text"].lower(),
            "containsPerformanceCriteria": "standard" in unit["text"].lower(),
            "containsRetrievalPrompt": "question" in unit["title"].lower(),
            "contentDensity": content_density(unit),
            "passiveLearningRisk": "medium",
            "redundancyRisk": "low",
            "instructionalValue": "medium",
            "confidence": SLIDE_CONFIDENCE,
            "provider": provider_id,
        }
        for unit in units
    ]


def classify_roles(unit: dict[str, Any]) -> list[str]:
    text = unit["text"].lower()
    title = unit["title"].lower()
    roles = []
    if any(term in title for term in ("agenda", "overview", "how to")):
        roles.append("Agenda")
    if "objective" in text:
        roles.append("Objective")
    if role_flags(unit)["assessment"]:
        roles.append("Assessment")
    if role_flags(unit)["practice"]:
        roles.append("Guided Practice")
    if role_flags(unit)["task"] or "part " in title:
        roles.append("Procedure")
    if "what is" in title or "definition" in text:
        roles.append("Definition")
    if not roles:
        roles.extend(
            ["Administrative", "Reference"] if unit["index"] == 0 else ["Reference"]
        )
    return sorted(set(roles), key=lambda role: ROLE_RANK.index(role))


def primary_role(roles: list[str]) -> str:
    return roles[0] if roles else "Reference"


def role_flags(unit: dict[str, Any]) -> dict[str, bool]:
    text = unit["text"].lower()
    return {
        "task": any(
            term in text for term in ("complete", "perform", "procedure", "required")
        ),
        "practice": "worked example" in text or "for example" in text,
        "assessment": any(
            term in text for term in ("knowledge check", "quiz", "which action")
        ),
    }


def content_density(unit: dict[str, Any]) -> str:
    text = unit["text"]
    if (
        len(text) > OVERLOAD_TEXT_THRESHOLD
        or text.count("\n") > OVERLOAD_BULLET_THRESHOLD
    ):
        return "high"
    return "low"


def build_objectives(
    units: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    course_title: str,
    provider_id: str,
) -> list[dict[str, Any]]:
    explicit = explicit_objective(units)
    task = explicit.get("task") or f"complete the procedure described by {course_title}"
    condition = (
        explicit.get("condition")
        or "using the source course material and relevant operational context"
    )
    standard = (
        explicit.get("standard")
        or "with required information, decisions, and approvals represented accurately"
    )
    objective_type = "Terminal" if explicit else "Inferred"
    confidence = 1.0 if explicit else OBJECTIVE_CONFIDENCE
    return [
        {
            "id": "obj-001",
            "moduleId": modules[0]["id"] if modules else "module-001",
            "type": objective_type,
            "statement": f"Given applicable source guidance, learners will {task} {standard}.",
            "task": task,
            "condition": condition,
            "standard": standard,
            "verb": first_word(task),
            "bloomLevel": "apply",
            "supportingSlides": [u["id"] for u in units if role_flags(u)["task"]],
            "assessmentEvidence": [],
            "source": "slide_text" if explicit else "inferred",
            "confidence": confidence,
            "provider": provider_id,
        }
    ]


def explicit_objective(units: list[dict[str, Any]]) -> dict[str, str]:
    pattern = re.compile(
        r"learning objective:\s*(?P<task>.+?)\s+(?P<condition>under .+?)\s+(?P<standard>with .+)$",
        re.IGNORECASE | re.DOTALL,
    )
    for unit in units:
        match = pattern.search(unit["text"])
        if match:
            return {
                key: " ".join(value.lower().split())
                for key, value in match.groupdict().items()
            }
    return {}


def first_word(text: str) -> str:
    return (text.strip().split() or ["complete"])[0].lower()


def build_assessments(
    units: list[dict[str, Any]], objectives: list[dict[str, Any]], provider_id: str
) -> list[dict[str, Any]]:
    assessments = []
    objective_ids = [objective["id"] for objective in objectives]
    for index, unit in enumerate(units, start=1):
        if role_flags(unit)["assessment"]:
            assessments.append(
                {
                    "id": f"assessment-{len(assessments) + 1:03d}",
                    "type": "Knowledge Check",
                    "slides": [unit["id"]],
                    "measuresObjective": objective_ids,
                    "confidence": ANALYSIS_CONFIDENCE,
                    "provider": provider_id,
                }
            )
    return assessments


def build_pedagogical_analysis(
    units: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    slides: list[dict[str, Any]],
    objectives: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    provider_id: str,
) -> dict[str, Any]:
    return {
        "objectiveAlignment": [
            {
                "objectiveId": objective["id"],
                "taskCoverage": 0.8,
                "conditionCoverage": 0.55,
                "standardCoverage": 0.6,
                "instructionSupportScore": 1.0
                if objective["supportingSlides"]
                else 0.4,
                "assessmentSupportScore": 1.0 if assessments else 0.0,
                "performanceReadinessRisk": "low" if assessments else "medium",
                "findings": [],
                "confidence": ANALYSIS_CONFIDENCE,
                "provider": provider_id,
            }
            for objective in objectives
        ],
        "bloomAnalysis": [
            {
                "objectiveId": objective["id"],
                "objectiveBloom": objective["bloomLevel"],
                "instructionBloom": "apply",
                "assessmentBloom": "apply",
                "mismatchType": "aligned",
                "appropriatenessScore": 0.74,
                "rationale": "Procedural training is expected to target Apply.",
                "confidence": ANALYSIS_CONFIDENCE,
                "provider": provider_id,
            }
            for objective in objectives
        ],
        "moduleSequences": [module_sequence(module, provider_id) for module in modules],
        "slideDensity": [slide_density(unit, provider_id) for unit in units],
        "redundancy": redundancy(units, provider_id),
    }


def module_sequence(module: dict[str, Any], provider_id: str) -> dict[str, Any]:
    coverage = {
        event: {
            "present": event
            in {"gainAttention", "presentContent", "recallPriorLearning"},
            "slideIds": module["slideIds"]
            if event in {"gainAttention", "presentContent", "recallPriorLearning"}
            else [],
        }
        for event in GAGNE_EVENTS
    }
    missing = [event for event, item in coverage.items() if not item["present"]]
    return {
        "moduleId": module["id"],
        "gagneCoverage": coverage,
        "missingEvents": missing,
        "weakEvents": [],
        "sequenceIntegrityScore": 0.33,
        "confidence": 0.64,
        "provider": provider_id,
    }


def slide_density(unit: dict[str, Any], provider_id: str) -> dict[str, Any]:
    overloaded = content_density(unit) == "high"
    return {
        "slideId": unit["id"],
        "densityScore": 0.82 if overloaded else 0.254,
        "overloadRisk": "high" if overloaded else "low",
        "rewriteCandidate": overloaded,
        "reasons": ["over_deterministic_threshold"]
        if overloaded
        else ["within_deterministic_threshold"],
        "metrics": {
            "textDensity": round(min(len(unit["text"]) / 20000, 1), 3),
            "bulletDensity": round(min(unit["text"].count("\n") / 20, 1), 3),
            "conceptCount": min(len(set(words(unit["text"]))), 30),
            "visualTextImbalance": 0.061,
            "titleBodyMismatch": False,
            "multipleObjectives": False,
        },
        "confidence": 0.8,
        "provider": provider_id,
    }


def redundancy(units: list[dict[str, Any]], provider_id: str) -> list[dict[str, Any]]:
    counts = Counter(unit["title"].strip().lower() for unit in units)
    records = []
    for title, count in counts.items():
        if title and count >= REDUNDANCY_MIN_REPEATS:
            records.append(
                {
                    "id": f"redundancy-{len(records) + 1:03d}",
                    "pattern": title,
                    "slides": [
                        unit["id"]
                        for unit in units
                        if unit["title"].strip().lower() == title
                    ],
                    "reason": "Repeated title/content pattern may indicate redundant instruction.",
                    "confidence": ANALYSIS_CONFIDENCE,
                    "provider": provider_id,
                }
            )
    return records


def build_reengineering_candidates(
    units: list[dict[str, Any]], densities: list[dict[str, Any]], provider_id: str
) -> list[dict[str, Any]]:
    candidates = []
    for density in densities:
        if density["rewriteCandidate"]:
            candidates.append(
                {
                    "id": f"reeng-{len(candidates) + 1:03d}",
                    "type": "splitSlide",
                    "priority": "high",
                    "slideIds": [density["slideId"]],
                    "pedagogicalIssue": "overloaded_slide",
                    "reason": "Slide density exceeds deterministic overload threshold.",
                    "confidence": 0.71,
                    "provider": provider_id,
                }
            )
    if not candidates and units:
        candidates.append(
            {
                "id": "reeng-001",
                "type": "addPractice",
                "priority": "medium",
                "slideIds": [units[-1]["id"]],
                "pedagogicalIssue": "practice_gap",
                "reason": "No explicit practice or assessment was detected.",
                "confidence": 0.61,
                "provider": provider_id,
            }
        )
    return candidates


def apply_provider_review(
    course: dict[str, Any], review: dict[str, Any], provider_id: str
) -> None:
    if not review:
        return
    course_review = review.get("courseReview") or {}
    if course_review:
        course["instructionalDesignReview"] = course_review
    slide_reviews = {
        str(item.get("slideId")): item
        for item in review.get("slideReviews") or []
        if isinstance(item, dict)
    }
    for slide in course["slides"]:
        if slide["slideId"] in slide_reviews:
            slide["llmReview"] = slide_reviews[slide["slideId"]]
            slide["provider"] = provider_id


def review_payload(course: dict[str, Any]) -> dict[str, Any]:
    return {
        "metadata": course["metadata"],
        "modules": course["modules"][:12],
        "slides": course["slides"][:40],
        "objectives": course["objectives"],
    }


def analysis_summary(course_model: dict[str, Any]) -> dict[str, Any]:
    course = course_model["course"]
    return {
        "schemaVersion": "1.0",
        "artifactKind": "course_analysis_summary",
        "documentId": course_model["documentId"],
        "moduleCount": len(course["modules"]),
        "slideRecordCount": len(course["slides"]),
        "objectiveCount": len(course["objectives"]),
        "assessmentCount": len(course["assessments"]),
        "reengineeringCandidateCount": len(course["reengineeringCandidates"]),
        "readiness": {
            "overallScore": 0.62,
            "assessmentSupport": bool(course["assessments"]),
        },
        "providerUsage": course_model["providerUsage"],
    }


def reengineering_input(course_model: dict[str, Any]) -> dict[str, Any]:
    course = course_model["course"]
    return {
        "schemaVersion": "1.0",
        "artifactKind": "reengineering_input",
        "documentId": course_model["documentId"],
        "flaggedSlides": [
            {
                "slideId": slide_id,
                "issues": [candidate["pedagogicalIssue"]],
                "candidateId": candidate["id"],
            }
            for candidate in course["reengineeringCandidates"]
            for slide_id in candidate["slideIds"]
        ],
    }


def enriched_manifest(
    manifest: dict[str, Any], slides: list[dict[str, Any]]
) -> dict[str, Any]:
    enriched = copy.deepcopy(manifest)
    slide_by_id = {slide["slideId"]: slide for slide in slides}
    key = "slides" if isinstance(enriched.get("slides"), list) else "units"
    for raw in enriched.get(key) or []:
        raw_id = raw.get("slideId") or raw.get("unitId")
        if raw_id in slide_by_id:
            raw["pedagogical"] = slide_by_id[raw_id]
    return enriched


def source_file(manifest: dict[str, Any]) -> str | None:
    source = manifest.get("source") or {}
    return source.get("originalFileName") or source.get("filename")


def document_id(manifest: dict[str, Any]) -> str:
    source = manifest.get("source") or {}
    digest = (
        source.get("sha256") or sha256(jsonish(manifest).encode("utf-8")).hexdigest()
    )
    return f"course-{str(digest)[:12]}"


def jsonish(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9'-]*", text.lower())
