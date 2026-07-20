"""Bedrock multimodal provider, routed through the LiteLLM proxy.

A thin wrapper that gives extractors a single way to send images + text to a
foundation model and get back text (or strict JSON). It deliberately knows
nothing about schematics, drawings, or any document type — callers own the
prompt. That keeps domain "understanding" in the model, not hard-coded in
Python.

All calls go through the local LiteLLM proxy (OpenAI chat-completions format),
which fronts Bedrock and owns credentials, guardrails, usage accounting, and
model aliasing. This service holds no Bedrock IAM permissions — only a LiteLLM
virtual key. Configuration comes from
:data:`docling_serve.settings.docling_serve_settings`
(``DOCLING_SERVE_LITELLM_*`` / ``DOCLING_SERVE_BEDROCK_*``), falling back to
the knowledge-graph proxy settings (``DOCLING_SERVE_GRAPH_LITELLM_*``) so a
single proxy endpoint + key serves both paths. When the provider is disabled
or the proxy is unreachable, calls raise :class:`BedrockUnavailableError` so
extractors can degrade gracefully instead of crashing the pipeline.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from docling_serve.identity import current_identity
from docling_serve.settings import docling_serve_settings

_log = logging.getLogger(__name__)

# Claude on Bedrock accepts png/jpeg/gif/webp. We render to PNG everywhere.
_SUPPORTED_IMAGE_FORMATS = {"png", "jpeg", "gif", "webp"}
# Defensive ceiling; individual models allow more, but large payloads are slow
# and expensive. Callers should down-scale before hitting this. (Base64 adds
# ~33% on the wire; this limit is on the raw bytes, matching the old direct
# Converse path.)
_MAX_IMAGE_BYTES = 4_500_000
# Dense documents (e.g. full-sheet schematics) can exceed max_tokens mid-answer.
# When the model stops on "length" we re-issue the request with the partial
# answer as prior assistant context followed by a user continuation turn.
# Current Bedrock Claude models reject a trailing assistant prefill, while the
# explicit user turn works across both old and new model families.
_MAX_CONTINUATION_ROUNDS = 4
_MAX_EMPTY_LENGTH_TOKEN_BUDGET = 32_768
_CONTINUE_PROMPT = (
    "Continue exactly where the previous response stopped. Return only the "
    "remaining content, with no preamble and without repeating prior content."
)
# Transport-level retry statuses (LiteLLM also retries upstream; this covers
# proxy restarts and throttling bubbling through).
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class BedrockUnavailableError(RuntimeError):
    """Raised when the provider is disabled, misconfigured, or the call failed.

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
    """Lazily-initialised LiteLLM-proxy client for Bedrock vision models.

    Thread-safe singleton-friendly: the HTTP client is created on first use and
    reused. Construct via :func:`get_bedrock_provider` rather than directly so
    settings overrides flow through consistently.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        vision_model: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._enabled = (
            enabled
            if enabled is not None
            else bool(getattr(docling_serve_settings, "bedrock_enabled", False))
        )
        self._base_url = (base_url or _resolved_base_url() or "").rstrip("/")
        self._api_key = api_key or _resolved_api_key()
        self._vision_model: str = str(
            vision_model
            or getattr(
                docling_serve_settings,
                "bedrock_vision_model",
                "bedrock-claude-sonnet-4-5",
            )
            or "bedrock-claude-sonnet-4-5"
        )
        self._max_tokens = max_tokens or int(
            getattr(docling_serve_settings, "bedrock_max_tokens", 8192)
        )
        self._reasoning_effort = (
            (
                reasoning_effort
                if reasoning_effort is not None
                else str(
                    getattr(
                        docling_serve_settings,
                        "bedrock_reasoning_effort",
                        "low",
                    )
                )
            )
            .strip()
            .lower()
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
                "Vision provider is disabled. Set DOCLING_SERVE_BEDROCK_ENABLED=true "
                "to use model-driven extractors."
            )
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            if not self._base_url:
                raise BedrockUnavailableError(
                    "LiteLLM proxy URL is not configured. Set "
                    "DOCLING_SERVE_LITELLM_BASE_URL (or "
                    "DOCLING_SERVE_GRAPH_LITELLM_BASE_URL)."
                )
            if not self._api_key:
                raise BedrockUnavailableError(
                    "LiteLLM API key is not configured. Set "
                    "DOCLING_SERVE_LITELLM_API_KEY (or "
                    "DOCLING_SERVE_GRAPH_LITELLM_API_KEY)."
                )
            import httpx

            self._client = httpx.Client(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=httpx.Timeout(
                    self._timeout_seconds,
                    connect=min(self._timeout_seconds, 20.0),
                ),
            )
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
        """Run one chat-completions exchange and return the assistant text.

        Raises :class:`BedrockUnavailableError` on any failure (disabled,
        transport, throttling, or empty output).
        """
        client = self._get_client()
        chat_messages: list[dict[str, Any]] = []
        if system:
            chat_messages.append({"role": "system", "content": system})
        chat_messages.extend(_to_chat_message(message) for message in messages)

        payload: dict[str, Any] = {
            "model": model_id or self._vision_model,
            "messages": chat_messages,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": (
                temperature if temperature is not None else self._temperature
            ),
        }
        if self._reasoning_effort in {"minimal", "low", "medium", "high"}:
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": self._reasoning_effort}
            # Adaptive thinking does not accept deterministic temperature.
            payload.pop("temperature", None)
        # Attribute the spend to the originating user/tenant when a request
        # identity is bound (deep-extraction tasks bind it from task metadata).
        # The call still authenticates with the docling-serve service key.
        identity = current_identity()
        tags = ["docling-vision"]
        request_headers = {}
        if identity is not None:
            if identity.end_user:
                payload["user"] = identity.end_user
            tags.extend(identity.tags())
            request_headers.update(identity.headers())
        request_headers["x-litellm-tags"] = ",".join(tags)

        parts: list[str] = []
        for _ in range(_MAX_CONTINUATION_ROUNDS + 1):
            try:
                data = self._post_chat_completion(
                    client, payload, headers=request_headers
                )
            except BedrockUnavailableError as err:
                if "thinking" not in payload or not _is_reasoning_parameter_error(err):
                    raise
                _log.warning(
                    "Vision model rejected adaptive reasoning parameters; retrying without them."
                )
                payload.pop("thinking", None)
                payload.pop("output_config", None)
                payload["temperature"] = (
                    temperature if temperature is not None else self._temperature
                )
                data = self._post_chat_completion(
                    client, payload, headers=request_headers
                )
            try:
                text, finish_reason = _extract_text(data)
            except BedrockUnavailableError as err:
                finish_reason = _finish_reason(data)
                current_budget = int(payload["max_tokens"])
                if (
                    str(err) == "Model returned an empty response."
                    and finish_reason == "length"
                    and current_budget < _MAX_EMPTY_LENGTH_TOKEN_BUDGET
                ):
                    next_budget = min(
                        current_budget * 2,
                        _MAX_EMPTY_LENGTH_TOKEN_BUDGET,
                    )
                    _log.warning(
                        "Model consumed the %s-token budget before emitting content; "
                        "retrying with %s tokens.",
                        current_budget,
                        next_budget,
                    )
                    payload["max_tokens"] = next_budget
                    continue
                raise
            parts.append(text)
            if finish_reason != "length":
                break
            # Preserve the partial answer as prior assistant context, but end
            # with a user message. Bedrock rejects trailing assistant prefill
            # for current Claude models.
            accumulated = "".join(parts).rstrip()
            parts = [accumulated]
            _log.info(
                "Model hit max_tokens after %s chars; continuing the response.",
                len(accumulated),
            )
            payload["messages"] = [
                *chat_messages,
                {"role": "assistant", "content": accumulated},
                {"role": "user", "content": _CONTINUE_PROMPT},
            ]
        else:
            _log.warning(
                "Model response still truncated after %s continuation rounds.",
                _MAX_CONTINUATION_ROUNDS,
            )

        return "".join(parts)

    def _post_chat_completion(
        self,
        client: Any,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        import httpx

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                time.sleep(min(2.0**attempt, 10.0))
            try:
                response = client.post(
                    "/chat/completions", json=payload, headers=headers
                )
            except httpx.HTTPError as err:
                last_error = err
                _log.warning(
                    "LiteLLM request failed (attempt %s/%s): %s",
                    attempt + 1,
                    self._max_retries + 1,
                    err,
                )
                continue
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = BedrockUnavailableError(
                    f"LiteLLM returned HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
                _log.warning(
                    "LiteLLM returned HTTP %s (attempt %s/%s).",
                    response.status_code,
                    attempt + 1,
                    self._max_retries + 1,
                )
                continue
            if response.status_code != 200:
                # Non-retryable (auth, guardrail block, bad request).
                raise BedrockUnavailableError(
                    f"LiteLLM returned HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            try:
                return response.json()
            except ValueError as err:
                raise BedrockUnavailableError(
                    f"LiteLLM returned a non-JSON response: {err}"
                ) from err
        raise BedrockUnavailableError(
            f"LiteLLM call failed after {self._max_retries + 1} attempts: {last_error}"
        ) from last_error

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
            messages=[
                VisionMessage(text=prompt, images=images, image_format=image_format)
            ],
            system=system,
            model_id=model_id,
            max_tokens=max_tokens,
        )
        parsed = _parse_json_object(text)
        if parsed is None:
            raise BedrockUnavailableError(
                "Model response did not contain a parseable JSON object."
            )
        return parsed


def _resolved_base_url() -> str | None:
    return getattr(docling_serve_settings, "litellm_base_url", None) or getattr(
        docling_serve_settings, "graph_litellm_base_url", None
    )


def _resolved_api_key() -> str | None:
    return getattr(docling_serve_settings, "litellm_api_key", None) or getattr(
        docling_serve_settings, "graph_litellm_api_key", None
    )


def _to_chat_message(message: VisionMessage) -> dict[str, Any]:
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
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/{image_format};base64,{encoded}"},
            }
        )
    content.append({"type": "text", "text": message.text})
    return {"role": "user", "content": content}


def _finish_reason(data: dict[str, Any]) -> str | None:
    choices = (data or {}).get("choices") or []
    if not choices:
        return None
    return (choices[0] or {}).get("finish_reason")


def _is_reasoning_parameter_error(error: Exception) -> bool:
    message = str(error).casefold()
    return any(
        token in message
        for token in (
            "thinking",
            "output_config",
            "reasoning_effort",
            "adaptive",
        )
    )


def _extract_text(data: dict[str, Any]) -> tuple[str, str | None]:
    choices = (data or {}).get("choices") or []
    if not choices:
        raise BedrockUnavailableError("LiteLLM returned no choices.")
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Some providers return content as a list of typed blocks.
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    else:
        text = (content or "").strip()
    if not text:
        raise BedrockUnavailableError("Model returned an empty response.")
    return text, choice.get("finish_reason")


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
    """Return the process-wide vision provider, creating it on first use."""
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
