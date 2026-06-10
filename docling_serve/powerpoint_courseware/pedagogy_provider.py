from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol


class BedrockConfigError(RuntimeError):
    pass


@dataclass
class ProviderUsage:
    provider: str
    llm_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "llmRequests": self.llm_requests,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.input_tokens + self.output_tokens,
            "estimatedCostUsd": round(self.estimated_cost_usd, 6),
        }


class PedagogyProvider(Protocol):
    provider_id: str
    usage: ProviderUsage

    def review_course(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class DeterministicPedagogyProvider:
    provider_id: str = "deterministic"
    usage: ProviderUsage = field(
        default_factory=lambda: ProviderUsage(provider="deterministic")
    )

    def review_course(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {}


@dataclass
class BedrockPedagogyProvider:
    model_id: str
    region: str
    client: Any | None = None
    max_tokens: int = 3500
    provider_id: str = "aws_bedrock_structured_output"
    usage: ProviderUsage = field(
        default_factory=lambda: ProviderUsage(provider="aws_bedrock_structured_output")
    )

    def review_course(self, payload: dict[str, Any]) -> dict[str, Any]:
        first = self._invoke(payload, "course")
        second = self._invoke(payload, "slides")
        return merge_reviews(first, second)

    def _invoke(self, payload: dict[str, Any], scope: str) -> dict[str, Any]:
        client = self.client or self._client()
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Review this PowerPoint courseware payload as JSON. "
                        f"Scope: {scope}.\n{json.dumps(payload, sort_keys=True)}"
                    ),
                }
            ],
        }
        response = client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(body).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        parsed = json.loads(response["body"].read().decode("utf-8"))
        usage = parsed.get("usage") or {}
        self.usage.llm_requests += 1
        self.usage.input_tokens += int(usage.get("input_tokens") or 0)
        self.usage.output_tokens += int(usage.get("output_tokens") or 0)
        return extract_json(anthropic_text(parsed))

    def _client(self) -> Any:
        import boto3  # type: ignore[import-not-found]

        return boto3.client("bedrock-runtime", region_name=self.region)


def merge_reviews(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    merged = dict(first)
    for key, value in second.items():
        if key not in merged or not merged[key]:
            merged[key] = value
    return merged


def anthropic_text(response: dict[str, Any]) -> str:
    chunks = []
    for item in response.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text") or ""))
    return "\n".join(chunks)


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return {}
        value = json.loads(stripped[start : end + 1])
    return value if isinstance(value, dict) else {}


def provider_from_environment(default: str = "deterministic") -> PedagogyProvider:
    provider = os.getenv("DOCLING_SERVE_COURSEWARE_PROVIDER") or os.getenv(
        "DOCLING_SERVE_COURSE_MODEL_PROVIDER", default
    )
    if provider in {"deterministic", "deterministic_fallback", ""}:
        return DeterministicPedagogyProvider()
    if provider in {"bedrock", "aws_bedrock_structured_output"}:
        model_id = os.getenv("DOCLING_SERVE_COURSEWARE_BEDROCK_MODEL_ID") or os.getenv(
            "DOCLING_SERVE_COURSE_MODEL_BEDROCK_MODEL_ID", ""
        )
        region = (
            os.getenv("DOCLING_SERVE_COURSEWARE_BEDROCK_REGION")
            or os.getenv("DOCLING_SERVE_COURSE_MODEL_BEDROCK_REGION")
            or os.getenv("AWS_REGION", "")
        )
        if not model_id:
            raise BedrockConfigError("Bedrock courseware provider requires a model id")
        if not region:
            raise BedrockConfigError("Bedrock courseware provider requires a region")
        return BedrockPedagogyProvider(model_id=model_id, region=region)
    raise BedrockConfigError(f"Unsupported courseware provider: {provider}")
