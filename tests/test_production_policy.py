from __future__ import annotations

import pytest
from pydantic import ValidationError

from docling_serve.settings import DoclingServeSettings


def _settings(**overrides) -> DoclingServeSettings:
    return DoclingServeSettings(_env_file=None, **overrides)


def test_secure_defaults_disable_remote_work_and_cross_origin_access():
    settings = _settings()
    assert settings.cors_origins == []
    assert settings.allow_default_tenant is False
    assert settings.bedrock_enabled is False
    assert settings.figure_hotspot_vision is False
    assert settings.vision_parts is False
    assert settings.technical_order_drawing_twin is False
    assert settings.graph_extraction_enabled is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"auth_mode": "none"}, "auth_mode=none"),
        ({"cors_origins": ["*"]}, "explicit CORS origin"),
        ({"allow_default_tenant": True}, "default tenant"),
        ({"allow_insecure_development": True}, "development exceptions"),
    ],
)
def test_production_rejects_insecure_posture(overrides, message):
    with pytest.raises(ValidationError, match=message):
        _settings(deployment_mode="production", **overrides)


@pytest.mark.parametrize(
    "feature",
    [
        "bedrock_enabled",
        "figure_hotspot_vision",
        "vision_parts",
        "technical_order_drawing_twin",
        "graph_extraction_enabled",
    ],
)
def test_remote_features_require_explicit_transport(feature):
    with pytest.raises(ValidationError, match="litellm_base_url"):
        _settings(**{feature: True})


def test_remote_features_accept_configured_transport():
    settings = _settings(
        graph_extraction_enabled=True,
        litellm_base_url="https://litellm.internal",
        litellm_api_key="secret",
    )
    assert settings.graph_extraction_enabled is True
