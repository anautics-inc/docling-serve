"""Leg 3 — AI courseware-improvement advisor.

Reads a Bloom-classified manifest and produces actionable suggestions for a
training author: per-unit improvements (move recall content toward practice,
add a missing learning objective) and deck-level findings (the deck never
reaches higher-order thinking, taxonomy confidence is low).

Provider-abstracted, same shape as the rest of the pipeline:
  - DeterministicAdvisor — rule-based, derived from the Bloom data already in
    the manifest; runs anywhere, no model.
  - BedrockAdvisor — sends slide content to Claude for concrete, content-
    specific rewrite suggestions; fail-open to the deterministic advisor.

Nothing hardcoded: model id, region, and provider choice are env-driven.
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol

HIGHER_ORDER = {"analyze", "evaluate", "create"}
_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


class AdvisorProvider(Protocol):
    provider_id: str

    def advise(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Return {deckLevel: [...], byUnit: {unitId: [...]}}."""


def _unit_text(unit: dict[str, Any]) -> str:
    parts = [unit.get("title") or ""]
    parts += [b.get("text") or "" for b in unit.get("blocks", [])]
    return "\n".join(p for p in parts if p.strip())


class DeterministicAdvisor:
    """Rule-based advice synthesized from the manifest's Bloom classifications."""

    provider_id = "deterministic_fallback"

    def advise(self, manifest: dict[str, Any]) -> dict[str, Any]:
        units = manifest.get("units", [])
        by_unit: dict[str, list[dict[str, Any]]] = {}
        for unit in units:
            suggestions: list[dict[str, Any]] = []
            classification = unit.get("classification") or {}
            level = classification.get("level")
            role = classification.get("role")

            # Recall-bound slide → suggest a practice task.
            if level in {"remember", "understand"} and role not in {"title", "summary_or_closing"}:
                suggestions.append(
                    {
                        "kind": "raise_bloom_level",
                        "detail": (
                            f"This {unit['unitType']} sits at '{level}'. Add a learner task or "
                            "worked example so it reaches 'apply'."
                        ),
                    }
                )
            # No explicit objective anywhere on the unit.
            roles = {(b.get("classification") or {}).get("role") for b in unit.get("blocks", [])}
            if "learning_objective" not in roles and role not in {"title", "summary_or_closing"}:
                suggestions.append(
                    {
                        "kind": "missing_objective",
                        "detail": "No learning-objective content detected — state what the learner should be able to do.",
                    }
                )
            # Surface per-block recommendations the classifier already produced.
            for block in unit.get("blocks", []):
                for rec in (block.get("classification") or {}).get("recommendedImprovements", []) or []:
                    suggestions.append(
                        {
                            "kind": "block_recommendation",
                            "blockId": block["blockId"],
                            "detail": rec.get("suggestion"),
                            "targetBloomLevel": rec.get("targetBloomLevel"),
                        }
                    )
            if suggestions:
                by_unit[unit["unitId"]] = suggestions

        deck_level: list[dict[str, Any]] = []
        summary = manifest.get("taxonomy", {}).get("deckSummary", {})
        if summary.get("higherOrderSlideCount", 0) == 0 and units:
            deck_level.append(
                {
                    "kind": "no_higher_order_content",
                    "detail": "The deck never moves beyond recall/understanding — add analyze, evaluate, or create activities.",
                }
            )
        fallback = summary.get("fallbackBlockFraction", 0.0)
        if fallback >= 0.8:
            deck_level.append(
                {
                    "kind": "low_confidence_taxonomy",
                    "detail": (
                        f"{fallback:.0%} of blocks were classified by keyword fallback — "
                        "run the Bedrock taxonomy provider for trustworthy levels."
                    ),
                }
            )
        return {"provider": self.provider_id, "deckLevel": deck_level, "byUnit": by_unit}


class BedrockAdvisor:
    """Claude-backed advisor — concrete, content-specific suggestions per unit."""

    provider_id = "aws_bedrock_advisor"

    def __init__(
        self, model_id: str, *, region_name: str | None = None,
        max_units: int = 60, fail_open: bool = True,
    ) -> None:
        self.model_id = model_id
        self.region_name = region_name
        self.max_units = max(1, max_units)  # cap per-call cost on huge decks
        self.fail_open = fail_open
        # AUDIT F8 — per-stage LLM usage ledger.
        from .usage_accounting import StageUsage

        self.usage = StageUsage(
            stage="advisor",
            provider=self.provider_id,
            model_id=model_id,
            region=region_name,
        )

    def advise(self, manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._advise(manifest)
        except Exception as err:  # noqa: BLE001 — fail-open by contract
            if not self.fail_open:
                raise
            fallback = DeterministicAdvisor().advise(manifest)
            fallback["provider"] = f"deterministic_fallback:bedrock_error:{type(err).__name__}"
            return fallback

    def _advise(self, manifest: dict[str, Any]) -> dict[str, Any]:
        import boto3  # type: ignore[import-not-found]
        from botocore.config import Config  # type: ignore[import-not-found]

        client = boto3.client(
            "bedrock-runtime",
            region_name=self.region_name,
            config=Config(read_timeout=120, connect_timeout=10,
                          retries={"max_attempts": 3, "mode": "standard"}),
        )
        units = manifest.get("units", [])[: self.max_units]
        targets = [
            {
                "unitId": u["unitId"],
                "title": u.get("title"),
                "bloomLevel": (u.get("classification") or {}).get("level"),
                "text": _unit_text(u)[:1200],
            }
            for u in units
        ]
        prompt = (
            "You are an instructional-design reviewer. For each training unit below, "
            "give 1-3 concrete, specific improvement suggestions: rewrites that raise the "
            "Bloom level, practice scenarios, or missing learning objectives. "
            "Return ONLY JSON: {\"byUnit\": {\"<unitId>\": [{\"kind\": str, \"detail\": str}]}, "
            "\"deckLevel\": [{\"kind\": str, \"detail\": str}]}."
        )
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4000,
            "temperature": 0,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt + "\n\n" + json.dumps({"units": targets})}
                ]}
            ],
        }
        response = client.invoke_model(
            modelId=self.model_id, contentType="application/json",
            accept="application/json", body=json.dumps(body).encode("utf-8"),
        )
        payload = json.loads(response["body"].read())
        from .usage_accounting import anthropic_tokens

        in_tok, out_tok = anthropic_tokens(payload)
        self.usage.record(input_tokens=in_tok, output_tokens=out_tok)
        text = "".join(
            item.get("text", "") for item in payload.get("content", []) or []
            if isinstance(item, dict) and item.get("type") == "text"
        )
        parsed = _extract_json(text)
        return {
            "provider": self.provider_id,
            "deckLevel": parsed.get("deckLevel", []),
            "byUnit": parsed.get("byUnit", {}),
        }


def _extract_json(text: str) -> dict[str, Any]:
    import re

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
        return {}


def provider_from_environment(default: str = "deterministic_fallback") -> AdvisorProvider:
    requested = (
        os.getenv("DOCLING_SERVE_DEEP_DOC_ADVISOR_PROVIDER")
        or os.getenv("CAPTIFY_DEEP_DOC_ADVISOR_PROVIDER")
        or default
    ).strip().lower()
    if requested in {"bedrock", "aws_bedrock_advisor"}:
        # AUDIT F2 — fail closed: explicit model/region config required.
        from .provider_config import resolve_bedrock_config

        model_id, region = resolve_bedrock_config(
            purpose="courseware-advisor",
            model_env_keys=(
                "DOCLING_SERVE_DEEP_DOC_ADVISOR_MODEL",
                "CAPTIFY_DEEP_DOC_ADVISOR_MODEL",
            ),
            default_model=_DEFAULT_MODEL,
        )
        max_units = int(os.getenv("DOCLING_SERVE_DEEP_DOC_ADVISOR_MAX_UNITS", "60"))
        return BedrockAdvisor(model_id, region_name=region, max_units=max_units)
    return DeterministicAdvisor()


def advise(manifest: dict[str, Any], provider: AdvisorProvider | None = None) -> dict[str, Any]:
    """Attach `manifest['coursewareAdvice']` and return it."""
    provider = provider or provider_from_environment()
    advice = provider.advise(manifest)
    advice["unitsWithSuggestions"] = len(advice.get("byUnit", {}))
    manifest["coursewareAdvice"] = advice
    return advice
