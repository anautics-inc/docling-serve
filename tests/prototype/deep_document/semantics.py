from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from . import bloom_classifier as bloom


@dataclass(frozen=True)
class DecisionContext:
    target_type: str
    target_id: str
    unit_id: str | None
    text: str
    kind: str | None = None
    placeholder_type: str | None = None
    index: int | None = None
    total: int | None = None
    structural_signals: dict[str, Any] | None = None


class SemanticDecisionProvider(Protocol):
    provider_id: str

    def classify(self, context: DecisionContext) -> dict[str, Any]:
        """Return a Bloom classification for a structure/content target."""

    def classify_many(self, contexts: list[DecisionContext]) -> dict[str, dict[str, Any]]:
        """Return classifications keyed by target_id."""


def _is_opaque(context: DecisionContext) -> bool:
    return (
        not context.text.strip()
        and (context.kind or "") in bloom.OPAQUE_BLOCK_KINDS
        and context.target_type == "block"
    )


class DeterministicFallbackProvider:
    provider_id = "deterministic_fallback"

    def classify(self, context: DecisionContext) -> dict[str, Any]:
        if context.target_type == "asset":
            return bloom.classify_picture_asset()
        if context.kind == "table":
            return bloom.classify_table(context.text)
        # PR2/M3: opaque structural blocks (chart_placeholder, smartart_placeholder,
        # group, other) with no readable text must NOT be classified by the verb
        # matcher. They are explicit "unknown" signals to downstream UIs.
        if _is_opaque(context):
            return bloom.classify_opaque_structural(context.kind or "other")
        return bloom.classify(
            context.text,
            source=self._evidence_source(context),
            placeholder_type=context.placeholder_type,
            kind=context.kind,
        )

    def classify_many(self, contexts: list[DecisionContext]) -> dict[str, dict[str, Any]]:
        return {context.target_id: self.classify(context) for context in contexts}

    def _evidence_source(self, context: DecisionContext) -> str:
        if context.target_type == "notes":
            return "speaker_notes"
        if context.target_type == "asset" or context.kind == "picture":
            return "image_caption"
        if context.kind == "table":
            return "table_headers"
        return "block_text"


class BedrockSemanticProvider:
    """Production-shaped GenAI provider.

    The contract is content-agnostic: only target content + structural signals
    are sent to the model. Opaque blocks are short-circuited deterministically
    before reaching Bedrock (M3 cost guard).
    """

    provider_id = "aws_bedrock_structured_output"

    def __init__(
        self,
        model_id: str,
        *,
        region_name: str | None = None,
        max_batch_targets: int = 10,
        fail_open: bool = True,
        max_calls: int | None = None,
    ) -> None:
        self.model_id = model_id
        self.region_name = region_name
        self.max_batch_targets = max(1, max_batch_targets)
        self.fail_open = fail_open
        # AUDIT F8 — per-document LLM call budget + per-stage usage ledger.
        from .usage_accounting import DEFAULT_MAX_LLM_CALLS, StageUsage

        self.max_calls = max_calls if max_calls is not None else DEFAULT_MAX_LLM_CALLS
        self.usage = StageUsage(
            stage="bloomClassification",
            provider=self.provider_id,
            model_id=model_id,
            region=region_name,
        )

    def classify(self, context: DecisionContext) -> dict[str, Any]:
        return self.classify_many([context])[context.target_id]

    def classify_many(self, contexts: list[DecisionContext]) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        model_contexts = []
        for context in contexts:
            # PR2: never spend tokens on opaque-empty blocks — fall back deterministically.
            if _is_opaque(context):
                results[context.target_id] = bloom.classify_opaque_structural(context.kind or "other")
                continue
            if not context.text.strip():
                results[context.target_id] = self._fallback_with_reason(context, "empty_target")
                continue
            model_contexts.append(context)

        for start in range(0, len(model_contexts), self.max_batch_targets):
            batch = model_contexts[start : start + self.max_batch_targets]
            # AUDIT F8 — per-document LLM call budget. Once the cap is hit, the
            # remaining targets are classified deterministically rather than
            # spending unbounded Bedrock calls on a pathologically large deck.
            if self.usage.requests >= self.max_calls:
                for context in batch:
                    results[context.target_id] = self._fallback_with_reason(
                        context, "budget_exceeded"
                    )
                continue
            try:
                results.update(self._classify_batch(batch))
            except Exception as err:
                if not self.fail_open:
                    raise
                for context in batch:
                    results[context.target_id] = self._fallback_with_reason(
                        context, f"bedrock_error:{type(err).__name__}"
                    )
        return results

    def _classify_batch(self, contexts: list[DecisionContext]) -> dict[str, dict[str, Any]]:
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]
        except ImportError:
            return {
                context.target_id: self._fallback_with_reason(context, "boto3_not_available")
                for context in contexts
            }

        client = boto3.client(
            "bedrock-runtime",
            region_name=self.region_name,
            config=Config(
                connect_timeout=float(os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_CONNECT_TIMEOUT", "10")),
                read_timeout=float(os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_READ_TIMEOUT", "60")),
                retries={
                    "max_attempts": int(os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_MAX_ATTEMPTS", "3")),
                    "mode": "standard",
                },
            ),
        )
        response = client.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(self._request_body(contexts)).encode("utf-8"),
        )
        payload = json.loads(response["body"].read())
        from .usage_accounting import anthropic_tokens

        in_tok, out_tok = anthropic_tokens(payload)
        self.usage.record(input_tokens=in_tok, output_tokens=out_tok)
        classifications = self._extract_classifications(payload)
        results: dict[str, dict[str, Any]] = {}
        for context in contexts:
            classification = classifications.get(context.target_id)
            if classification:
                classification["provider"] = self.provider_id
                classification["method"] = f"bedrock:{self.model_id}"
                classification.setdefault("recommendedImprovements", [])
                classification.setdefault("evidence", [])
                results[context.target_id] = classification
            elif self.fail_open:
                results[context.target_id] = self._fallback_with_reason(
                    context, "bedrock_missing_target"
                )
            else:
                raise RuntimeError(f"Bedrock response omitted target {context.target_id}")
        return results

    def _request_body(self, contexts: list[DecisionContext]) -> dict[str, Any]:
        schema = {
            "classifications": [
                {
                    "targetId": "string exactly matching an input targetId",
                    "level": bloom.BLOOM_LEVELS,
                    "role": bloom.INSTRUCTIONAL_ROLES,
                    "confidence": "number 0..1",
                    "evidence": [
                        {
                            "source": "block_text|speaker_notes|table_headers|image_caption|aggregate",
                            "excerpt": "short excerpt from supplied input only",
                            "terms": ["matched or salient terms"],
                            "reason": "short reason or null",
                        }
                    ],
                    "recommendedImprovements": [
                        {"targetBloomLevel": "string", "suggestion": "string"}
                    ],
                }
            ]
        }
        prompt = (
            "Classify each document target using Bloom's revised taxonomy. "
            "Do not infer from known deck templates or fixed logos. Use only the target content "
            "and structural signals supplied here. Return only JSON matching the schema. "
            "Every input targetId must appear exactly once in classifications."
        )
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 3500,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "instruction": prompt,
                                    "schema": schema,
                                    "targets": [
                                        {
                                            "type": context.target_type,
                                            "targetId": context.target_id,
                                            "unitId": context.unit_id,
                                            "kind": context.kind,
                                            "placeholderType": context.placeholder_type,
                                            "text": context.text[:1600],
                                            "structuralSignals": context.structural_signals or {},
                                        }
                                        for context in contexts
                                    ],
                                },
                                sort_keys=True,
                            ),
                        }
                    ],
                }
            ],
        }

    def _extract_classifications(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                text = item.get("text") if isinstance(item, dict) else None
                if not text:
                    continue
                candidate = self._parse_json_text(text)
                classifications = candidate.get("classifications") if isinstance(candidate, dict) else None
                if isinstance(classifications, list):
                    results = {}
                    for item in classifications:
                        if not isinstance(item, dict):
                            continue
                        target_id = item.get("targetId") or item.get("target_id") or item.get("id")
                        item = self._normalize_classification(item)
                        if isinstance(target_id, str) and item.get("level") in bloom.BLOOM_LEVELS:
                            item.pop("targetId", None)
                            results[target_id] = item
                    if results:
                        return results
        return {}

    def _parse_json_text(self, text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}

    def _normalize_classification(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        for key in ("level", "role"):
            value = normalized.get(key)
            if isinstance(value, list) and value:
                normalized[key] = value[0]
        confidence = normalized.get("confidence", 0.0)
        try:
            normalized["confidence"] = max(0.0, min(float(confidence), 1.0))
        except (TypeError, ValueError):
            normalized["confidence"] = 0.0
        if normalized.get("role") not in bloom.INSTRUCTIONAL_ROLES:
            normalized["role"] = "reference"
        return normalized

    def _fallback_with_reason(self, context: DecisionContext, reason: str) -> dict[str, Any]:
        classification = DeterministicFallbackProvider().classify(context)
        classification["provider"] = "deterministic_fallback"
        classification["method"] = f"{classification['method']}:{reason}"
        self.usage.note_fallback()  # AUDIT F8 — a fallback is not a billed call
        return classification


def provider_from_environment(default: str = "deterministic_fallback") -> SemanticDecisionProvider:
    requested = (
        os.getenv("DOCLING_SERVE_DEEP_DOC_SEMANTIC_PROVIDER")
        or os.getenv("CAPTIFY_DEEP_DOC_SEMANTIC_PROVIDER")
        or default
    ).strip().lower()
    if requested in {"bedrock", "aws_bedrock_structured_output"}:
        # AUDIT F2 — fail closed: explicit model/region config required.
        from .provider_config import resolve_bedrock_config

        model_id, region_name = resolve_bedrock_config(
            purpose="bloom-classification",
            model_env_keys=(
                "DOCLING_SERVE_DEEP_DOC_BEDROCK_MODEL",
                "CAPTIFY_DEEP_DOC_BEDROCK_MODEL",
            ),
            default_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        )
        max_batch_targets = int(os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_BATCH_SIZE", "10"))
        fail_open = (
            os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_FAIL_OPEN")
            or os.getenv("CAPTIFY_DEEP_DOC_BEDROCK_FAIL_OPEN")
            or "true"
        ).lower() in {"1", "true", "yes", "on"}
        max_calls_env = os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_MAX_CALLS")
        return BedrockSemanticProvider(
            model_id,
            region_name=region_name,
            max_batch_targets=max_batch_targets,
            fail_open=fail_open,
            max_calls=int(max_calls_env) if max_calls_env else None,
        )
    return DeterministicFallbackProvider()


def annotation_id(target_type: str, target_id: str, classification: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "targetType": target_type,
            "targetId": target_id,
            "level": classification.get("level"),
            "role": classification.get("role"),
            "provider": classification.get("provider"),
            "method": classification.get("method"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sem-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def apply_semantic_annotations(
    units: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    provider: SemanticDecisionProvider,
) -> dict[str, Any]:
    annotations: list[dict[str, Any]] = []
    total = len(units)
    contexts: list[DecisionContext] = []
    block_contexts: dict[str, tuple[dict[str, Any], DecisionContext]] = {}
    notes_contexts: dict[str, tuple[dict[str, Any], DecisionContext]] = {}
    asset_contexts: dict[str, tuple[dict[str, Any], DecisionContext]] = {}

    for unit in units:
        for block in unit["blocks"]:
            # PR2/m2: do NOT fall back to shapeName/assetId/kind as classification text.
            # Structural metadata is carried separately in structural_signals; the
            # text field is genuinely empty for opaque blocks, and _is_opaque routes
            # them to classify_opaque_structural in the provider.
            context = DecisionContext(
                target_type="block",
                target_id=block["blockId"],
                unit_id=unit["unitId"],
                text=block.get("text") or "",
                kind=block.get("kind"),
                placeholder_type=block.get("placeholderType"),
                structural_signals={
                    "bbox": block.get("bbox"),
                    "zOrder": block.get("zOrder"),
                    "relationshipId": block.get("relationshipId"),
                    "doclingPictureId": block.get("doclingPictureId"),
                    "tableRef": block.get("tableRef"),
                    "shapeName": block.get("shapeName"),
                    "assetId": block.get("assetId"),
                },
            )
            contexts.append(context)
            block_contexts[context.target_id] = (block, context)

        # PR2/M4: empty speaker notes do not get classified. Spec says
        # `speakerNotes.classification` is null when `cleaned` is null.
        notes = unit["speakerNotes"]
        if notes.get("cleaned"):
            notes_context = DecisionContext(
                target_type="notes",
                target_id=f"{unit['unitId']}:speakerNotes",
                unit_id=unit["unitId"],
                text=notes["cleaned"],
                kind="speaker_notes",
                structural_signals={
                    "rawPresent": bool(notes.get("raw")),
                    "junkFiltered": notes.get("junkFiltered"),
                },
            )
            contexts.append(notes_context)
            notes_contexts[notes_context.target_id] = (notes, notes_context)
        else:
            notes["classification"] = None
            notes["semanticAnnotationId"] = None

    for asset in assets:
        context = DecisionContext(
            target_type="asset",
            target_id=asset["assetId"],
            unit_id=None,
            text="",
            kind=asset.get("kind"),
            structural_signals={
                "mimeType": asset.get("mimeType"),
                "sizeBytes": asset.get("sizeBytes"),
                "sourceParts": asset.get("sourceParts", []),
                "usedByCount": len(asset.get("usedBy", [])),
            },
        )
        contexts.append(context)
        asset_contexts[context.target_id] = (asset, context)

    classifications = provider.classify_many(contexts)

    for target_id, (block, context) in block_contexts.items():
        classification = classifications[target_id]
        block["classification"] = classification
        block["semanticAnnotationId"] = _append_annotation(annotations, context, classification)

    for target_id, (notes, context) in notes_contexts.items():
        classification = classifications[target_id]
        notes["classification"] = classification
        notes["semanticAnnotationId"] = _append_annotation(annotations, context, classification)

    for target_id, (asset, context) in asset_contexts.items():
        classification = classifications[target_id]
        asset["classification"] = classification
        asset["semanticAnnotationId"] = _append_annotation(annotations, context, classification)

    # PR1/m3: deleted the dead self-assignment loop that existed in experiment2.
    for unit in units:
        block_classifications = [block["classification"] for block in unit["blocks"]]
        notes_classification = unit["speakerNotes"].get("classification")

        slide_text = "\n".join(
            part
            for part in [
                unit.get("title") or "",
                "\n".join((block.get("text") or "") for block in unit["blocks"]),
                unit["speakerNotes"].get("cleaned") or "",
            ]
            if part.strip()
        )
        unit_classification = bloom.aggregate_classification(
            [*block_classifications, notes_classification],
            slide_text,
            source="aggregate",
            index=unit["index"],
            total=total,
        )
        unit["classification"] = unit_classification
        unit_context = DecisionContext(
            target_type="unit",
            target_id=unit["unitId"],
            unit_id=unit["unitId"],
            text=slide_text,
            kind="slide",
            index=unit["index"],
            total=total,
            structural_signals={
                "slideNumber": unit.get("slideNumber"),
                "layoutName": unit.get("layoutName"),
                "blockCount": len(unit["blocks"]),
                "hasDecorations": bool(unit.get("decorations")),
            },
        )
        unit["semanticAnnotationId"] = _append_annotation(annotations, unit_context, unit_classification)

    return {
        "semanticSchemaVersion": "1.0",
        "sourceOfTruth": "semanticAnnotations",
        "denormalizedFields": ["units[].classification", "units[].blocks[].classification", "assets[].classification"],
        "decisionPolicy": {
            "provider": provider.provider_id,
            "selection": "CAPTIFY_DEEP_DOC_SEMANTIC_PROVIDER or deterministic_fallback",
            "contentAgnostic": True,
            "notes": (
                "Structure extraction never assumes fixed deck titles, logos, layouts, or template names. "
                "Semantic decisions are made per target from content excerpts and structural signals."
            ),
        },
        "annotations": annotations,
    }


def _append_annotation(
    annotations: list[dict[str, Any]],
    context: DecisionContext,
    classification: dict[str, Any],
) -> str:
    sem_id = annotation_id(context.target_type, context.target_id, classification)
    annotations.append(
        {
            "annotationId": sem_id,
            "annotationType": "bloom_classification",
            "target": {
                "targetType": context.target_type,
                "targetId": context.target_id,
                "unitId": context.unit_id,
            },
            "provider": classification.get("provider"),
            "classification": classification,
            "inputs": {
                "kind": context.kind,
                "placeholderType": context.placeholder_type,
                "structuralSignals": context.structural_signals or {},
                "textExcerpt": bloom.excerpt(context.text),
            },
        }
    )
    return sem_id
