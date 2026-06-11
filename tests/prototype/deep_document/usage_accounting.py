"""AUDIT F8 — per-extraction LLM usage and cost accounting.

Every Bedrock-backed stage records token usage and call counts into a
`StageUsage`. `build_usage_block()` aggregates the stages into the
`manifest['usage']` record so each document answers: what did this cost, why,
and which stage/model drove it.

Cost is deliberately NOT computed from hardcoded prices. `estimatedCostUsd`
stays null unless a config-driven price table is supplied — Bedrock pricing
varies by model, region, and effective date and must not be baked into
extraction code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Per-document safety ceilings. These are *defaults*; production resolves the
# real values from the environment via budget_from_environment() (AUDIT F3).
DEFAULT_MAX_LLM_CALLS = 400
DEFAULT_MAX_TOTAL_TOKENS = 2_000_000

# Config keys a production pricing table must supply for dollar-cost estimation
# (AUDIT F4 — cost is deferred, never hardcoded; this names the future source).
PRICING_CONFIG_KEYS = [
    "modelId",
    "region",
    "effectiveDate",
    "inputTokenPriceUsdPerMillion",
    "outputTokenPriceUsdPerMillion",
    "imageUnitPriceUsd",
]


def budget_from_environment() -> tuple[int, int]:
    """Resolve (max_llm_calls, max_total_tokens) from the environment.

    AUDIT F3 — the orchestrator must prove its budgets came from ATO config,
    not from a hardcoded constant. Falls back to the documented defaults.
    """
    calls = os.getenv("DOCLING_SERVE_DEEP_DOC_MAX_LLM_CALLS")
    tokens = os.getenv("DOCLING_SERVE_DEEP_DOC_MAX_TOTAL_TOKENS")
    return (
        int(calls) if calls else DEFAULT_MAX_LLM_CALLS,
        int(tokens) if tokens else DEFAULT_MAX_TOTAL_TOKENS,
    )


@dataclass
class StageUsage:
    """Accumulates LLM usage for one pipeline stage (vision / bloom / advisor)."""

    stage: str
    provider: str = "deterministic_fallback"
    model_id: str | None = None
    region: str | None = None
    requests: int = 0
    retries: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_count: int = 0
    images: int = 0

    def record(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        retries: int = 0,
        failed: bool = False,
        fallback: bool = False,
        images: int = 0,
    ) -> None:
        self.requests += 1
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.retries += int(retries or 0)
        self.failures += 1 if failed else 0
        self.fallback_count += 1 if fallback else 0
        self.images += int(images or 0)

    def note_fallback(self) -> None:
        """Record a deterministic fallback — NOT a billed LLM call."""
        self.fallback_count += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "provider": self.provider,
            "modelId": self.model_id,
            "region": self.region,
            "requests": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "fallbackCount": self.fallback_count,
            "images": self.images,
        }


def build_usage_block(
    stages: list[StageUsage],
    *,
    unit_count: int,
    size_bytes: int,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
    max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS,
) -> dict[str, Any]:
    """Aggregate stage usage into the manifest `usage` record."""
    total_in = sum(s.input_tokens for s in stages)
    total_out = sum(s.output_tokens for s in stages)
    total = total_in + total_out
    # LLM calls = requests from non-deterministic stages only.
    llm_calls = sum(s.requests for s in stages if s.provider != "deterministic_fallback")
    mb = (size_bytes or 0) / 1_048_576

    return {
        "byStage": {s.stage: s.as_dict() for s in stages},
        "totals": {
            "llmCalls": llm_calls,
            "requests": sum(s.requests for s in stages),
            "retries": sum(s.retries for s in stages),
            "failures": sum(s.failures for s in stages),
            "fallbackCount": sum(s.fallback_count for s in stages),
            "inputTokens": total_in,
            "outputTokens": total_out,
            "totalTokens": total,
        },
        "normalized": {
            "tokensPerUnit": round(total / unit_count, 1) if unit_count else 0.0,
            "tokensPerMB": round(total / mb, 1) if mb else 0.0,
        },
        # AUDIT F4: dollar cost is DEFERRED, not complete. Token/request
        # accounting above is implemented; cost stays null until a price table
        # is configured. `pricingConfigKeys` names the future config source —
        # prices must never be hardcoded in extraction code.
        "estimatedCostUsd": None,
        "pricingConfigured": False,
        "costStatus": "deferred",
        "pricingConfigKeys": list(PRICING_CONFIG_KEYS),
        "budget": {
            "maxLlmCalls": max_llm_calls,
            "maxTotalTokens": max_total_tokens,
            "llmCallsUsed": llm_calls,
            "totalTokensUsed": total,
            "exceeded": llm_calls > max_llm_calls or total > max_total_tokens,
        },
    }


def anthropic_tokens(payload: dict[str, Any]) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from a Bedrock Anthropic response."""
    usage = payload.get("usage") or {}
    return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
