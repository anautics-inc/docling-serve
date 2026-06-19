"""Model providers available to every extractor.

Extractors must not embed vendor SDK calls or model-specific prompt plumbing
directly. They depend on the small provider surface defined here so the same
extraction code can run against Bedrock today and another backend later.

Currently exposes the Bedrock multimodal provider used by the schematic /
drawing extractors to *understand* documents (no hard-coded symbol rules).
"""

from __future__ import annotations

from docling_serve.providers.bedrock import (
    BedrockProvider,
    BedrockUnavailableError,
    VisionMessage,
    get_bedrock_provider,
)

__all__ = [
    "BedrockProvider",
    "BedrockUnavailableError",
    "VisionMessage",
    "get_bedrock_provider",
]
