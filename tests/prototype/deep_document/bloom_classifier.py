from __future__ import annotations

import re
from collections import Counter
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
    "remember": {"define", "identify", "list", "name", "recall", "recognize", "select", "state"},
    "understand": {"classify", "describe", "discuss", "explain", "interpret", "summarize", "understand"},
    "apply": {"apply", "calculate", "complete", "demonstrate", "execute", "implement", "perform", "solve", "use"},
    "analyze": {"analyze", "compare", "contrast", "differentiate", "distinguish", "examine", "organize"},
    "evaluate": {"assess", "critique", "evaluate", "judge", "justify", "prioritize", "recommend", "validate", "verify"},
    "create": {"build", "compose", "create", "design", "develop", "formulate", "generate", "publish", "write"},
}

INSTRUCTIONAL_ROLES = [
    "title",
    "learning_objective",
    "concept_explanation",
    "procedure",
    "example",
    "knowledge_check",
    "summary_or_closing",
    "reference",
    "appendix",
]

ROLE_PATTERNS = [
    ("learning_objective", re.compile(r"\b(objective|learners? will|by the end|able to)\b", re.I)),
    ("knowledge_check", re.compile(r"\b(quiz|question|check|exercise|practice|scenario)\b", re.I)),
    ("procedure", re.compile(r"\b(step|process|procedure|complete|fill|submit|validate|block|action)\b", re.I)),
    ("example", re.compile(r"\b(example|sample|case study|scenario)\b", re.I)),
    ("appendix", re.compile(r"\b(appendix|backup)\b", re.I)),
    ("reference", re.compile(r"\b(reference|source|manual|bibliography|further reading|iaw|according to)\b", re.I)),
    ("summary_or_closing", re.compile(r"\b(summary|recap|closing|conclusion|next steps)\b", re.I)),
]

OPAQUE_BLOCK_KINDS = {"chart_placeholder", "smartart_placeholder", "group", "other"}

VALID_PROVIDERS = {"deterministic_fallback", "aws_bedrock_structured_output"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def excerpt(text: str, max_len: int = 180) -> str:
    squashed = re.sub(r"\s+", " ", text).strip()
    if len(squashed) <= max_len:
        return squashed
    return squashed[: max_len - 1].rstrip() + "..."


def slide_position_role(*, index: int, total: int) -> str | None:
    """Positional hint for slide-level aggregation only.

    Not called from per-block classification. PR1 fix: removing this from
    the block-level path prevents footer/decoration blocks on slide 1 / slide N
    from inheriting `title` / `summary_or_closing` roles unrelated to content.
    """
    if total and total >= 2:
        if index == 0:
            return "title"
        if index == total - 1:
            return "summary_or_closing"
    return None


def classify_role(
    text: str,
    *,
    placeholder_type: str | None = None,
    kind: str | None = None,
) -> str:
    """Per-block role classification — content + placeholder + kind only.

    PR1: dropped `index`/`total` parameters. Slide position is a
    slide-level signal, not a block-level signal. See `slide_position_role`
    and `aggregate_classification` for how it's applied at the right layer.
    """
    if placeholder_type in {"title", "ctrTitle"}:
        return "title"
    if placeholder_type == "subTitle":
        return "concept_explanation"
    if kind == "picture":
        return "reference"
    if kind == "table":
        return "reference"
    for role, pattern in ROLE_PATTERNS:
        if pattern.search(text):
            return role
    return "concept_explanation" if text.strip() else "reference"


def classify(
    text: str,
    *,
    source: str,
    placeholder_type: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    role = classify_role(text, placeholder_type=placeholder_type, kind=kind)
    token_set = _words(text)
    scored: list[tuple[int, int, str, list[str]]] = []
    for rank, level in enumerate(BLOOM_LEVELS):
        hits = sorted(token_set & BLOOM_VERBS[level])
        scored.append((len(hits), rank, level, hits))

    hit_count, _rank, level, hits = max(scored, key=lambda item: (item[0], item[1]))
    if hit_count == 0:
        level = "understand" if text.strip() else "remember"
        confidence = 0.28 if text.strip() else 0.18
        method = "default_no_direct_bloom_verb"
    else:
        confidence = min(0.55 + hit_count * 0.08, 0.84)
        method = "deterministic_bloom_verb_match"

    improvement = improvement_for(level, role)
    return {
        "level": level,
        "role": role,
        "confidence": round(confidence, 2),
        "provider": "deterministic_fallback",
        "method": method,
        "evidence": [
            {
                "source": source,
                "excerpt": excerpt(text),
                "terms": hits[:8],
                "reason": None if hits else "No direct Bloom verb was found; fallback rule applied.",
            }
        ],
        "recommendedImprovements": [improvement] if improvement else [],
    }


def classify_table(text: str, *, source: str = "table_headers") -> dict[str, Any]:
    classification = classify(text, source=source, kind="table")
    if text.strip():
        classification["level"] = "analyze"
        classification["confidence"] = max(float(classification["confidence"]), 0.4)
        classification["method"] = "table_shape_heuristic"
        classification["evidence"][0]["reason"] = (
            "Table-like content usually asks learners to compare or inspect structured fields."
        )
    else:
        classification["level"] = "understand"
        classification["confidence"] = 0.3
        classification["method"] = "table_no_text_heuristic"
        classification["evidence"][0]["reason"] = (
            "OOXML exposed a table block but no text was available in the block payload."
        )
    return classification


def classify_picture_asset(source_parts: list[str] | None = None) -> dict[str, Any]:
    """PR2/m1 fix: no longer feeds joined source-part paths into the verb matcher.

    Excerpt is empty; reason carries the explanation. Confidence stays low
    until a VLM caption provider is wired in.
    """
    return {
        "level": "understand",
        "role": "reference",
        "confidence": 0.2,
        "provider": "deterministic_fallback",
        "method": "no_caption_available",
        "evidence": [
            {
                "source": "image_caption",
                "excerpt": "",
                "terms": [],
                "reason": "No VLM caption is available; image needs human or Bedrock review.",
            }
        ],
        "recommendedImprovements": [],
    }


def classify_opaque_structural(kind: str) -> dict[str, Any]:
    """PR2/M3 fix: chart/SmartArt/group/other blocks without readable text.

    Confidence 0.0 is deliberate. We do not know the Bloom level — emitting
    `understand@0.28` would have looked like a real classification to any
    consumer that did not read the `method` field.
    """
    return {
        "level": "understand",
        "role": "reference",
        "confidence": 0.0,
        "provider": "deterministic_fallback",
        "method": "opaque_structural_no_content",
        "evidence": [
            {
                "source": "block_text",
                "excerpt": "",
                "terms": [],
                "reason": (
                    f"{kind} block has no readable text in OOXML; "
                    "needs VLM caption or human review."
                ),
            }
        ],
        "recommendedImprovements": [],
    }


def improvement_for(level: str, role: str) -> dict[str, str] | None:
    if role in {"title", "summary_or_closing", "reference", "appendix"}:
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
            "suggestion": "Add a decision point where learners justify readiness or choose among alternatives.",
        }
    return None


def aggregate_classification(
    classifications: list[dict[str, Any] | None],
    text: str,
    *,
    source: str,
    index: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    """Aggregate child classifications into the slide-level classification.

    PR1: `index`/`total` are accepted here (slide-level is the right layer
    for positional hints). The positional role wins ONLY when the aggregated
    `dominant_role` is weak (`concept_explanation`/`reference`).

    PR2: tolerates `None` entries (empty speaker notes) by filtering them
    before aggregation.
    """
    valid = [c for c in classifications if c is not None]
    if not valid:
        # Fall back on text-based classification when nothing useful to aggregate.
        result = classify(text, source=source)
        positional = (
            slide_position_role(index=index, total=total)
            if index is not None and total is not None
            else None
        )
        if positional and result["role"] in {"concept_explanation", "reference"}:
            result["role"] = positional
        return result

    rank = {level: i for i, level in enumerate(BLOOM_LEVELS)}
    role_counts = Counter(item["role"] for item in valid)
    dominant_level = max((item["level"] for item in valid), key=lambda level: rank[level])
    dominant_role = role_counts.most_common(1)[0][0]
    confidences = [float(item.get("confidence", 0.0)) for item in valid]

    positional = (
        slide_position_role(index=index, total=total)
        if index is not None and total is not None
        else None
    )
    if positional and dominant_role in {"concept_explanation", "reference"}:
        dominant_role = positional

    return {
        "level": dominant_level,
        "role": dominant_role,
        "confidence": round(sum(confidences) / len(confidences), 2),
        "provider": "deterministic_fallback",
        "method": "aggregate_child_classifications",
        "evidence": [
            {
                "source": source,
                "excerpt": excerpt(text),
                "terms": [],
                "reason": "Aggregated as the highest Bloom level present across child blocks and speaker notes.",
            }
        ],
        "recommendedImprovements": [],
    }


def taxonomy_summary(units: list[dict[str, Any]], assets: list[dict[str, Any]]) -> dict[str, Any]:
    block_classifications: list[dict[str, Any]] = []
    all_leaf_classifications: list[dict[str, Any]] = []
    for unit in units:
        blocks = [block["classification"] for block in unit.get("blocks", [])]
        block_classifications.extend(blocks)
        all_leaf_classifications.extend(blocks)
        notes_classification = unit["speakerNotes"].get("classification")
        if notes_classification is not None:
            all_leaf_classifications.append(notes_classification)
    all_leaf_classifications.extend(asset["classification"] for asset in assets)

    bloom_distribution = Counter(item["level"] for item in block_classifications)
    role_distribution = Counter(item["role"] for item in block_classifications)
    confidences = [float(item.get("confidence", 0.0)) for item in all_leaf_classifications]
    fallback_count = sum(
        1
        for item in block_classifications
        if item.get("method") in {"default_no_direct_bloom_verb", "opaque_structural_no_content"}
    )
    provider_counts = Counter(item.get("provider") for item in all_leaf_classifications)
    provider_total = sum(provider_counts.values()) or 1
    higher_order = {"analyze", "evaluate", "create"}

    return {
        "taxonomy": "bloom_revised",
        "taxonomyVersion": "1.0",
        "classificationProvider": "deterministic_fallback",
        "levels": [
            {"id": level, "label": level.title(), "definition": BLOOM_DEFINITIONS[level]}
            for level in BLOOM_LEVELS
        ],
        "instructionalRoles": INSTRUCTIONAL_ROLES,
        "deckSummary": {
            "slideCount": len(units),
            "blockCount": sum(len(unit.get("blocks", [])) for unit in units),
            "bloomDistribution": {level: bloom_distribution.get(level, 0) for level in BLOOM_LEVELS},
            "roleDistribution": {role: role_distribution.get(role, 0) for role in INSTRUCTIONAL_ROLES},
            "dominantBloomLevel": bloom_distribution.most_common(1)[0][0] if bloom_distribution else None,
            "higherOrderBlockCount": sum(
                1
                for unit in units
                for block in unit.get("blocks", [])
                if block["classification"]["level"] in higher_order
            ),
            "higherOrderSlideCount": sum(
                1 for unit in units if unit["classification"]["level"] in higher_order
            ),
            "lowestConfidence": round(min(confidences), 2) if confidences else 0.0,
            "averageConfidence": round(sum(confidences) / len(confidences), 2) if confidences else 0.0,
            "providerCoverage": {
                "deterministic_fallback": round(
                    provider_counts.get("deterministic_fallback", 0) / provider_total, 4
                ),
                "aws_bedrock_structured_output": round(
                    provider_counts.get("aws_bedrock_structured_output", 0) / provider_total, 4
                ),
            },
            "fallbackBlockFraction": round(fallback_count / len(block_classifications), 4)
            if block_classifications
            else 0.0,
        },
    }
