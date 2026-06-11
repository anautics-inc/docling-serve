from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BLOOM_LEVELS = ["remember", "understand", "apply", "analyze", "evaluate", "create"]

BLOOM_DEFINITIONS = {
    "remember": "Recall facts, terms, definitions, procedures, or basic concepts.",
    "understand": "Explain ideas, summarize meaning, interpret content, or describe relationships.",
    "apply": "Use knowledge in a procedure, scenario, task, or worked example.",
    "analyze": "Break material into parts, compare, categorize, diagnose, or identify relationships.",
    "evaluate": "Judge quality, validate readiness, critique, justify, or make a recommendation.",
    "create": "Produce a new artifact, design a plan, compose content, or generate a solution.",
}

BLOOM_VERBS = {
    "remember": {
        "define",
        "identify",
        "list",
        "name",
        "recall",
        "recognize",
        "select",
        "state",
    },
    "understand": {
        "classify",
        "describe",
        "discuss",
        "explain",
        "interpret",
        "summarize",
        "understand",
    },
    "apply": {
        "apply",
        "calculate",
        "complete",
        "demonstrate",
        "execute",
        "implement",
        "perform",
        "solve",
        "use",
    },
    "analyze": {
        "analyze",
        "compare",
        "contrast",
        "differentiate",
        "distinguish",
        "examine",
        "organize",
    },
    "evaluate": {
        "assess",
        "critique",
        "evaluate",
        "judge",
        "justify",
        "prioritize",
        "recommend",
        "validate",
        "verify",
    },
    "create": {
        "build",
        "compose",
        "create",
        "design",
        "develop",
        "formulate",
        "generate",
        "publish",
        "write",
    },
}

ROLE_PATTERNS = [
    ("title", re.compile(r"\b(welcome|overview|introduction|agenda)\b", re.I)),
    ("learning_objective", re.compile(r"\b(objective|learners? will|by the end|able to)\b", re.I)),
    ("knowledge_check", re.compile(r"\b(quiz|question|check|exercise|practice|scenario)\b", re.I)),
    ("procedure", re.compile(r"\b(step|process|procedure|complete|fill|submit|validate|block|action)\b", re.I)),
    ("reference", re.compile(r"\b(reference|appendix|source|manual|bibliography|further reading)\b", re.I)),
]


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def text_excerpt(text: str, max_len: int = 180) -> str:
    squashed = re.sub(r"\s+", " ", text).strip()
    if len(squashed) <= max_len:
        return squashed
    return squashed[: max_len - 1].rstrip() + "..."


def classify_role(text: str, index: int, total: int) -> str:
    if index == 0:
        return "title"
    if index == total - 1:
        return "summary_or_closing"
    for role, pattern in ROLE_PATTERNS:
        if pattern.search(text):
            return role
    if len(text.split()) < 12:
        return "section_header"
    return "concept_explanation"


def classify_bloom(text: str) -> dict[str, Any]:
    token_set = words(text)
    scored: list[tuple[int, int, str, list[str]]] = []
    for rank, level in enumerate(BLOOM_LEVELS):
        hits = sorted(token_set & BLOOM_VERBS[level])
        scored.append((len(hits), rank, level, hits))
    hit_count, rank, level, hits = max(scored, key=lambda item: (item[0], item[1]))
    if hit_count == 0:
        level = "understand"
        hits = []
        confidence = 0.28
        method = "default_no_direct_bloom_verb"
    else:
        confidence = min(0.55 + hit_count * 0.08, 0.84)
        method = "deterministic_bloom_verb_match"
    return {
        "level": level,
        "definition": BLOOM_DEFINITIONS[level],
        "confidence": round(confidence, 2),
        "evidenceTerms": hits[:8],
        "method": method,
    }


def improvement_for(level: str, role: str) -> dict[str, str] | None:
    if role in {"title", "summary_or_closing"}:
        return None
    if level in {"remember", "understand"}:
        return {
            "targetBloomLevel": "apply",
            "suggestion": "Add a learner task, scenario, or worked example that requires using this content.",
        }
    if level == "apply":
        return {
            "targetBloomLevel": "analyze",
            "suggestion": "Ask learners to compare cases, diagnose errors, or explain why a step is required.",
        }
    if level == "analyze":
        return {
            "targetBloomLevel": "evaluate",
            "suggestion": "Add a review checklist or decision point where learners justify readiness.",
        }
    return None


def unit_text(unit: dict[str, Any]) -> str:
    parts = [
        str(unit.get("title") or ""),
        str(unit.get("text") or ""),
        str(unit.get("speakerNotes") or ""),
    ]
    return "\n".join(part for part in parts if part.strip())


def build_taxonomy(manifest: dict[str, Any]) -> dict[str, Any]:
    units = manifest.get("units", [])
    classifications = []
    for unit in units:
        text = unit_text(unit)
        role = classify_role(text, int(unit.get("index", 0)), len(units))
        bloom = classify_bloom(text)
        recommendation = improvement_for(bloom["level"], role)
        classifications.append(
            {
                "unitId": unit["unitId"],
                "unitType": unit.get("unitType", "slide"),
                "index": unit.get("index"),
                "title": unit.get("title"),
                "instructionalRole": role,
                "bloomLevel": bloom["level"],
                "bloomDefinition": bloom["definition"],
                "confidence": bloom["confidence"],
                "method": bloom["method"],
                "evidence": [
                    {
                        "source": "slide_text_and_notes",
                        "terms": bloom["evidenceTerms"],
                        "excerpt": text_excerpt(text),
                    }
                ],
                "recommendedImprovements": [recommendation] if recommendation else [],
            }
        )

    distribution = Counter(item["bloomLevel"] for item in classifications)
    roles = Counter(item["instructionalRole"] for item in classifications)
    return {
        "taxonomy": "bloom_revised",
        "levels": [
            {"id": level, "label": level.title(), "definition": BLOOM_DEFINITIONS[level]}
            for level in BLOOM_LEVELS
        ],
        "classificationProvider": "deterministic_fallback",
        "productionProvider": {
            "type": "aws_bedrock_structured_output",
            "recommendedModels": ["anthropic.claude-4.5-sonnet"],
            "notes": (
                "Use Bedrock structured outputs or strict tool schema. "
                "Fallback classifier remains for tests and model outage."
            ),
        },
        "deckSummary": {
            "unitCount": len(classifications),
            "bloomDistribution": dict(distribution),
            "roleDistribution": dict(roles),
            "dominantBloomLevel": distribution.most_common(1)[0][0] if distribution else None,
            "higherOrderCount": sum(
                distribution[level] for level in ["analyze", "evaluate", "create"]
            ),
        },
        "unitClassifications": classifications,
    }


def widget_for_unit(unit: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    index = int(unit.get("index", 0))
    columns = 3
    col = index % columns
    row = index // columns
    render = unit.get("render") or {}
    return {
        "widgetId": f"deep-doc-{unit['unitId']}",
        "wikiItemId": unit.get("sourceItemId", "prototype-source-item"),
        "builtinType": "pptx-slide",
        "title": f"{index + 1}. {unit.get('title') or unit['unitId']}",
        "x": 80 + col * 460,
        "y": 80 + row * 360,
        "w": 420,
        "h": 300,
        "builtinPayload": {
            "slideIndex": index,
            "slideImageRef": render.get("imagePath"),
            "slideText": unit.get("text"),
            "speakerNotes": unit.get("speakerNotes"),
            "sourceUnitId": unit["unitId"],
            "sourceFileName": Path(unit.get("sourcePart", unit["unitId"])).name,
            "bloomLevel": classification["bloomLevel"],
            "instructionalRole": classification["instructionalRole"],
            "taxonomyConfidence": classification["confidence"],
        },
    }


def shape_for_widget(widget: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    color_by_level = {
        "remember": "light-blue",
        "understand": "blue",
        "apply": "green",
        "analyze": "yellow",
        "evaluate": "orange",
        "create": "violet",
    }
    return {
        "id": f"shape:{widget['widgetId']}",
        "type": "geo",
        "x": widget["x"],
        "y": widget["y"],
        "props": {
            "w": widget["w"],
            "h": widget["h"],
            "geo": "rectangle",
            "color": color_by_level.get(classification["bloomLevel"], "grey"),
            "labelColor": "black",
            "fill": "semi",
            "dash": "draw",
            "size": "s",
            "richText": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"{widget['title']}\n"
                                    f"Bloom: {classification['bloomLevel']} | "
                                    f"Role: {classification['instructionalRole']}"
                                ),
                            }
                        ],
                    }
                ],
            },
        },
        "meta": {
            "projectionKind": "deep-document-slide",
            "unitId": widget["builtinPayload"]["sourceUnitId"],
            "widgetId": widget["widgetId"],
        },
    }


def build_canvas_projection(manifest: dict[str, Any], taxonomy: dict[str, Any]) -> dict[str, Any]:
    classifications = {
        item["unitId"]: item for item in taxonomy["unitClassifications"]
    }
    widgets = []
    shapes = []
    for unit in manifest.get("units", []):
        classification = classifications[unit["unitId"]]
        widget = widget_for_unit(unit, classification)
        widgets.append(widget)
        shapes.append(shape_for_widget(widget, classification))
    return {
        "target": "captify_tldraw",
        "projectionVersion": "1.0-prototype",
        "layout": {
            "columns": 3,
            "widgetSize": {"w": 420, "h": 300},
            "origin": {"x": 80, "y": 80},
            "gap": {"x": 40, "y": 60},
        },
        "widgets": widgets,
        "tldrawCommands": [
            {
                "operation": "createShapes",
                "shapes": shapes,
            },
            {"operation": "zoomToFit"},
        ],
    }


def bedrock_schema() -> dict[str, Any]:
    return {
        "name": "bloom_training_taxonomy_classification",
        "schema": {
            "type": "object",
            "required": [
                "unitId",
                "instructionalRole",
                "bloomLevel",
                "confidence",
                "evidence",
                "recommendedImprovements",
            ],
            "properties": {
                "unitId": {"type": "string"},
                "instructionalRole": {
                    "type": "string",
                    "enum": [
                        "title",
                        "learning_objective",
                        "concept_explanation",
                        "procedure",
                        "example",
                        "knowledge_check",
                        "summary_or_closing",
                        "reference",
                        "appendix",
                    ],
                },
                "bloomLevel": {"type": "string", "enum": BLOOM_LEVELS},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["source", "excerpt", "reason"],
                        "properties": {
                            "source": {"type": "string"},
                            "excerpt": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "recommendedImprovements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["targetBloomLevel", "suggestion"],
                        "properties": {
                            "targetBloomLevel": {"type": "string", "enum": BLOOM_LEVELS},
                            "suggestion": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def build_artifact(manifest: dict[str, Any]) -> dict[str, Any]:
    taxonomy = build_taxonomy(manifest)
    canvas = build_canvas_projection(manifest, taxonomy)
    return {
        "schemaVersion": "1.0-prototype",
        "artifactKind": "deep_document_training_canvas",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "source": manifest.get("source"),
        "documentType": manifest.get("documentType"),
        "deepManifest": manifest,
        "trainingTaxonomy": taxonomy,
        "canvasProjection": canvas,
        "bedrockStructuredOutputSchema": bedrock_schema(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    artifact = build_artifact(manifest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2))
    print(
        json.dumps(
            {
                "artifact": str(args.out),
                "units": len(artifact["deepManifest"].get("units", [])),
                "widgets": len(artifact["canvasProjection"]["widgets"]),
                "bloomDistribution": artifact["trainingTaxonomy"]["deckSummary"][
                    "bloomDistribution"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
