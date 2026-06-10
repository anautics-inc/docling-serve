"""Amazon Bedrock multimodal provider.

A thin, dependency-light wrapper over the Bedrock ``Converse`` API that gives
extractors a single way to send images + text to a foundation model and get
back text (or strict JSON). It deliberately knows nothing about schematics,
drawings, or any document type — callers own the prompt. That keeps domain
"understanding" in the model, not hard-coded in Python.

Configuration comes from :data:`docling_serve.settings.docling_serve_settings`
(``DOCLING_SERVE_BEDROCK_*``) with AWS credentials resolved by the standard
boto3 chain (env vars, shared config, or instance role). When Bedrock is
disabled or unreachable, calls raise :class:`BedrockUnavailableError` so
extractors can degrade gracefully instead of crashing the pipeline.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from docling_serve.settings import docling_serve_settings

_log = logging.getLogger(__name__)

# Bedrock Converse accepts png/jpeg/gif/webp. We render to PNG everywhere.
_SUPPORTED_IMAGE_FORMATS = {"png", "jpeg", "gif", "webp"}
# Defensive ceiling; individual models allow more, but large payloads are slow
# and expensive. Callers should down-scale before hitting this.
_MAX_IMAGE_BYTES = 4_500_000
# Dense documents (e.g. full-sheet schematics) can exceed maxTokens mid-answer.
# When the model stops on "max_tokens" we re-issue the request with the partial
# answer as an assistant prefill so it resumes exactly where it stopped.
_MAX_CONTINUATION_ROUNDS = 4


class BedrockUnavailableError(RuntimeError):
    """Raised when Bedrock is disabled, misconfigured, or the call failed.

    Extractors catch this and fall back to non-model behaviour so a missing or
    throttled model never fails the whole extraction job.
    """


@dataclass(slots=True)
class VisionMessage:
    """One multimodal turn: free text plus zero or more rendered page images."""

    text: str
    images: list[bytes] = field(default_factory=list)
    image_format: str = "png"


class BedrockProvider:
    """Lazily-initialised Bedrock Converse client.

    Thread-safe singleton-friendly: the boto3 client is created on first use and
    reused. Construct via :func:`get_bedrock_provider` rather than directly so
    settings overrides flow through consistently.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        region: str | None = None,
        vision_model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._enabled = (
            enabled
            if enabled is not None
            else bool(getattr(docling_serve_settings, "bedrock_enabled", False))
        )
        self._region = region or _resolved_region()
        self._vision_model = vision_model or getattr(
            docling_serve_settings,
            "bedrock_vision_model",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        )
        self._max_tokens = max_tokens or int(
            getattr(docling_serve_settings, "bedrock_max_tokens", 8192)
        )
        self._temperature = (
            temperature
            if temperature is not None
            else float(getattr(docling_serve_settings, "bedrock_temperature", 0.0))
        )
        self._timeout_seconds = timeout_seconds or float(
            getattr(docling_serve_settings, "bedrock_timeout_seconds", 120.0)
        )
        self._max_retries = max_retries or int(
            getattr(docling_serve_settings, "bedrock_max_retries", 3)
        )
        self._client: Any = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def vision_model(self) -> str:
        return self._vision_model

    def _get_client(self) -> Any:
        if not self._enabled:
            raise BedrockUnavailableError(
                "Bedrock is disabled. Set DOCLING_SERVE_BEDROCK_ENABLED=true to use "
                "model-driven extractors."
            )
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                import boto3
                from botocore.config import Config
            except ImportError as err:  # pragma: no cover - boto3 is a hard dep
                raise BedrockUnavailableError(
                    "boto3 is not installed; cannot reach Bedrock."
                ) from err
            try:
                self._client = boto3.client(
                    "bedrock-runtime",
                    region_name=self._region,
                    config=Config(
                        read_timeout=self._timeout_seconds,
                        connect_timeout=min(self._timeout_seconds, 20.0),
                        retries={"max_attempts": self._max_retries, "mode": "adaptive"},
                    ),
                )
            except Exception as err:  # pragma: no cover - environment dependent
                raise BedrockUnavailableError(
                    f"Failed to create Bedrock client in region {self._region!r}: {err}"
                ) from err
        return self._client

    def converse(
        self,
        *,
        messages: list[VisionMessage],
        system: str | None = None,
        model_id: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Run one Converse exchange and return the assistant text.

        Raises :class:`BedrockUnavailableError` on any failure (disabled,
        transport, throttling, or empty output).
        """
        client = self._get_client()
        bedrock_messages = [_to_bedrock_message(message) for message in messages]
        request: dict[str, Any] = {
            "modelId": model_id or self._vision_model,
            "messages": bedrock_messages,
            "inferenceConfig": {
                "maxTokens": max_tokens or self._max_tokens,
                "temperature": (
                    temperature if temperature is not None else self._temperature
                ),
            },
        }
        if system:
            request["system"] = [{"text": system}]

        parts: list[str] = []
        for _ in range(_MAX_CONTINUATION_ROUNDS + 1):
            try:
                response = client.converse(**request)
            except Exception as err:
                raise BedrockUnavailableError(
                    f"Bedrock converse call failed: {err}"
                ) from err
            parts.append(_extract_text(response))
            if response.get("stopReason") != "max_tokens":
                break
            # Assistant prefill: the model resumes mid-token from the partial
            # text, so plain concatenation reconstructs the full answer.
            # Converse rejects prefill with trailing whitespace, so strip it
            # consistently from both the prefill and the accumulated answer
            # (whitespace there is insignificant outside string literals).
            accumulated = "".join(parts).rstrip()
            parts = [accumulated]
            _log.info(
                "Bedrock hit maxTokens after %s chars; continuing the response.",
                len(accumulated),
            )
            request["messages"] = [
                *bedrock_messages,
                {"role": "assistant", "content": [{"text": accumulated}]},
            ]
        else:
            _log.warning(
                "Bedrock response still truncated after %s continuation rounds.",
                _MAX_CONTINUATION_ROUNDS,
            )

        return "".join(parts)

    def understand_json(
        self,
        *,
        prompt: str,
        images: list[bytes],
        system: str | None = None,
        image_format: str = "png",
        model_id: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send images + a prompt and parse the model's reply as a JSON object.

        The prompt is responsible for instructing the model to return JSON; this
        method only locates and parses it. Raises
        :class:`BedrockUnavailableError` when no JSON object can be recovered.
        """
        text = self.converse(
            messages=[VisionMessage(text=prompt, images=images, image_format=image_format)],
            system=system,
            model_id=model_id,
            max_tokens=max_tokens,
        )
        parsed = _parse_json_object(text)
        if parsed is None:
            raise BedrockUnavailableError(
                "Bedrock response did not contain a parseable JSON object."
            )
        return parsed


def _resolved_region() -> str:
    import os

    return (
        getattr(docling_serve_settings, "bedrock_region", None)
        or getattr(docling_serve_settings, "deep_document_s3_region", None)
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )


def _to_bedrock_message(message: VisionMessage) -> dict[str, Any]:
    image_format = message.image_format.lower()
    if image_format == "jpg":
        image_format = "jpeg"
    if image_format not in _SUPPORTED_IMAGE_FORMATS:
        raise BedrockUnavailableError(
            f"Unsupported image format {message.image_format!r}; "
            f"expected one of {sorted(_SUPPORTED_IMAGE_FORMATS)}."
        )
    content: list[dict[str, Any]] = []
    for image_bytes in message.images:
        if len(image_bytes) > _MAX_IMAGE_BYTES:
            raise BedrockUnavailableError(
                f"Rendered image is {len(image_bytes)} bytes, over the "
                f"{_MAX_IMAGE_BYTES} byte limit; render at a lower DPI."
            )
        content.append(
            {"image": {"format": image_format, "source": {"bytes": image_bytes}}}
        )
    content.append({"text": message.text})
    return {"role": "user", "content": content}


def _extract_text(response: dict[str, Any]) -> str:
    output = (response or {}).get("output") or {}
    message = output.get("message") or {}
    blocks = message.get("content") or []
    parts = [block.get("text", "") for block in blocks if isinstance(block, dict)]
    text = "".join(parts).strip()
    if not text:
        raise BedrockUnavailableError("Bedrock returned an empty response.")
    return text


def _parse_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    # Strip ```json ... ``` fences the model may add.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced {...} span.
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start : index + 1]
                try:
                    value = json.loads(candidate)
                    return value if isinstance(value, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


_provider: BedrockProvider | None = None
_provider_lock = threading.Lock()


def get_bedrock_provider() -> BedrockProvider:
    """Return the process-wide Bedrock provider, creating it on first use."""
    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
        if _provider is None:
            _provider = BedrockProvider()
    return _provider


def reset_bedrock_provider() -> None:
    """Drop the cached provider (used by tests that override settings)."""
    global _provider
    with _provider_lock:
        _provider = None
