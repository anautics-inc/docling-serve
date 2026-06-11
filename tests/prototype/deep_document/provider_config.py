"""AUDIT F2 — fail-closed Bedrock provider configuration.

When a Bedrock provider is requested, the model id and region must come from
explicit configuration. A missing ATO environment variable must fail fast —
not silently select a specific model/region — so deployment drift is caught
at startup.

Documented developer-convenience defaults are still available, but ONLY when
`DOCLING_SERVE_DEEP_DOC_ALLOW_DEFAULTS` is explicitly truthy. In production
deep mode that flag is unset, so provider construction fails closed.
"""
from __future__ import annotations

import os

ALLOW_DEFAULTS_ENV = "DOCLING_SERVE_DEEP_DOC_ALLOW_DEFAULTS"
REGION_ENV_KEYS = (
    "DOCLING_SERVE_DEEP_DOC_AWS_REGION",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


class BedrockConfigError(RuntimeError):
    """Raised when a Bedrock provider is requested without required config."""


def defaults_allowed() -> bool:
    return (os.getenv(ALLOW_DEFAULTS_ENV, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env(keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def resolve_bedrock_config(
    *,
    purpose: str,
    model_env_keys: tuple[str, ...],
    default_model: str,
    default_region: str = "us-east-1",
) -> tuple[str, str]:
    """Resolve (model_id, region) for a Bedrock provider, failing closed.

    Returns the explicitly-configured values. If either is missing and the
    dev-defaults flag is not set, raises `BedrockConfigError` with an
    actionable message — the provider must NOT be constructed.
    """
    model = _first_env(model_env_keys)
    region = _first_env(REGION_ENV_KEYS)
    if model and region:
        return model, region

    if defaults_allowed():
        return model or default_model, region or default_region

    missing: list[str] = []
    if not model:
        missing.append(" / ".join(model_env_keys))
    if not region:
        missing.append(" / ".join(REGION_ENV_KEYS))
    raise BedrockConfigError(
        f"Bedrock {purpose} provider requested but required configuration is "
        f"missing: {'; '.join(missing)}. Set these environment variables for "
        f"production, or set {ALLOW_DEFAULTS_ENV}=1 to use local development "
        f"defaults."
    )
