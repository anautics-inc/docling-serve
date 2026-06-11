"""Vision-LLM captioning of extracted picture assets.

Picture blocks (charts, diagrams, screenshots, photos) carry no readable text.
A vision model captions them so downstream Bloom classification and instructor
review have something to work with.

Provider-abstracted, mirroring `semantics.py`:
  - DeterministicCaptioner   — default; no network, emits an explicit
                               "needs review" placeholder so the pipeline
                               runs anywhere without AWS.
  - BedrockVisionCaptioner   — Claude vision via Bedrock; fail-open.

Nothing is hardcoded: model id, region, prompt, token budget, and the
provider choice are all environment-driven parameters.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Protocol


# Source of the picture binary on disk — the Docling-extracted image uri.
_DEFAULT_VISION_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
_DEFAULT_PROMPT = (
    "Describe this image from a training slide in one or two sentences. "
    "State what it depicts and any text, labels, or data visible. "
    "Be factual; do not speculate beyond what is shown."
)


class CaptionProvider(Protocol):
    provider_id: str

    def caption(self, image_bytes: bytes, mime_type: str | None) -> dict[str, Any]:
        """Return a caption dict for one image."""


class DeterministicCaptioner:
    """No-network fallback. Emits an explicit placeholder, never a fake caption."""

    provider_id = "deterministic_fallback"

    def caption(self, image_bytes: bytes, mime_type: str | None) -> dict[str, Any]:
        return {
            "text": None,
            "provider": self.provider_id,
            "method": "no_vision_provider",
            "reason": "No vision provider configured; image needs human or Bedrock review.",
        }


class BedrockVisionCaptioner:
    """Claude vision via Bedrock. Fail-open: any error degrades to a placeholder."""

    provider_id = "aws_bedrock_vision"

    def __init__(
        self,
        model_id: str,
        *,
        region_name: str | None = None,
        prompt: str = _DEFAULT_PROMPT,
        max_tokens: int = 300,
        fail_open: bool = True,
    ) -> None:
        self.model_id = model_id
        self.region_name = region_name
        self.prompt = prompt
        self.max_tokens = max(1, max_tokens)
        self.fail_open = fail_open
        # AUDIT F8 — per-stage LLM usage ledger.
        from .usage_accounting import StageUsage

        self.usage = StageUsage(
            stage="visionCaptioning",
            provider=self.provider_id,
            model_id=model_id,
            region=region_name,
        )

    def caption(self, image_bytes: bytes, mime_type: str | None) -> dict[str, Any]:
        try:
            return self._caption(image_bytes, mime_type)
        except Exception as err:  # noqa: BLE001 — fail-open by contract
            if not self.fail_open:
                raise
            fallback = DeterministicCaptioner().caption(image_bytes, mime_type)
            fallback["method"] = f"{fallback['method']}:bedrock_error:{type(err).__name__}"
            return fallback

    def _caption(self, image_bytes: bytes, mime_type: str | None) -> dict[str, Any]:
        import json

        import boto3  # type: ignore[import-not-found]
        from botocore.config import Config  # type: ignore[import-not-found]

        client = boto3.client(
            "bedrock-runtime",
            region_name=self.region_name,
            config=Config(
                connect_timeout=float(os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_CONNECT_TIMEOUT", "10")),
                read_timeout=float(os.getenv("DOCLING_SERVE_DEEP_DOC_BEDROCK_READ_TIMEOUT", "60")),
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        media_type = mime_type or "image/png"
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": self.prompt},
                    ],
                }
            ],
        }
        response = client.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body).encode("utf-8"),
        )
        payload = json.loads(response["body"].read())
        from .usage_accounting import anthropic_tokens

        in_tok, out_tok = anthropic_tokens(payload)
        self.usage.record(input_tokens=in_tok, output_tokens=out_tok, images=1)
        text = ""
        for item in payload.get("content", []) or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text += item.get("text", "")
        return {
            "text": text.strip() or None,
            "provider": self.provider_id,
            "method": f"bedrock_vision:{self.model_id}",
            "reason": None,
        }


def provider_from_environment(default: str = "deterministic_fallback") -> CaptionProvider:
    requested = (
        os.getenv("DOCLING_SERVE_DEEP_DOC_VISION_PROVIDER")
        or os.getenv("CAPTIFY_DEEP_DOC_VISION_PROVIDER")
        or default
    ).strip().lower()
    if requested in {"bedrock", "aws_bedrock_vision"}:
        # AUDIT F2 — fail closed: explicit model/region config required.
        from .provider_config import resolve_bedrock_config

        model_id, region_name = resolve_bedrock_config(
            purpose="vision-captioning",
            model_env_keys=(
                "DOCLING_SERVE_DEEP_DOC_VISION_MODEL",
                "CAPTIFY_DEEP_DOC_VISION_MODEL",
            ),
            default_model=_DEFAULT_VISION_MODEL,
        )
        max_tokens = int(os.getenv("DOCLING_SERVE_DEEP_DOC_VISION_MAX_TOKENS", "300"))
        prompt = os.getenv("DOCLING_SERVE_DEEP_DOC_VISION_PROMPT") or _DEFAULT_PROMPT
        return BedrockVisionCaptioner(
            model_id, region_name=region_name, prompt=prompt, max_tokens=max_tokens
        )
    return DeterministicCaptioner()


def caption_assets(
    manifest: dict[str, Any], provider: CaptionProvider | None = None
) -> dict[str, Any]:
    """Caption every picture asset in a manifest in place.

    Reads each picture binary from its `localPath` (the Docling-extracted
    image). Missing files degrade to a placeholder — never a crash. Returns
    coverage counts.
    """
    provider = provider or provider_from_environment()
    captioned = 0
    skipped = 0
    placeholder = 0
    for asset in manifest.get("assets", []):
        if asset.get("kind") != "picture":
            continue
        local_path = asset.get("localPath")
        if not local_path or not Path(local_path).exists():
            asset["caption"] = {
                "text": None,
                "provider": "deterministic_fallback",
                "method": "image_file_missing",
                "reason": "Picture binary not found on disk.",
            }
            skipped += 1
            continue
        image_bytes = Path(local_path).read_bytes()
        caption = provider.caption(image_bytes, asset.get("mimeType"))
        asset["caption"] = caption
        if caption.get("text"):
            captioned += 1
        else:
            placeholder += 1

    total = captioned + placeholder + skipped
    return {
        "pictureAssets": total,
        "captioned": captioned,
        "placeholder": placeholder,
        "skipped": skipped,
        "provider": provider.provider_id,
    }
