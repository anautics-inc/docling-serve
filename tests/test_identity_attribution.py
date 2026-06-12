"""Caller-identity threading: task metadata -> ContextVar -> LiteLLM payload."""

from __future__ import annotations

from typing import Any

from docling_serve.identity import (
    RequestIdentity,
    bind_identity,
    current_identity,
    identity_from_task_metadata,
)
from docling_serve.providers.bedrock import BedrockProvider, VisionMessage


def test_identity_from_task_metadata_roundtrip():
    identity = identity_from_task_metadata(
        {"tenant_id": "anautics", "actor_id": "user-123", "request_id": "req-9"}
    )
    assert identity == RequestIdentity(
        tenant_id="anautics", actor_id="user-123", request_id="req-9"
    )
    assert identity.end_user == "user-123"
    assert identity.tags() == ["tenant:anautics", "actor:user-123"]


def test_identity_from_task_metadata_empty_is_none():
    assert identity_from_task_metadata(None) is None
    assert identity_from_task_metadata({}) is None
    assert identity_from_task_metadata({"extraction": "deep"}) is None


def test_end_user_falls_back_to_tenant():
    identity = RequestIdentity(tenant_id="anautics")
    assert identity.end_user == "tenant:anautics"


def test_bind_identity_scopes_the_contextvar():
    assert current_identity() is None
    identity = RequestIdentity(tenant_id="t", actor_id="a")
    with bind_identity(identity):
        assert current_identity() is identity
    assert current_identity() is None


class _FakeResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, Any]:
        return {
            "choices": [
                {"message": {"content": "ok"}, "finish_reason": "stop"}
            ]
        }


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    def post(self, path: str, json: dict[str, Any], headers=None) -> _FakeResponse:
        self.calls.append((path, json, headers))
        return _FakeResponse()


def _provider_with_fake_client() -> tuple[BedrockProvider, _FakeClient]:
    provider = BedrockProvider(
        enabled=True,
        base_url="http://litellm.test/v1",
        api_key="sk-test",
        vision_model="bedrock-claude-sonnet-4-5",
    )
    fake = _FakeClient()
    provider._client = fake
    return provider, fake


def test_provider_attributes_bound_identity():
    provider, fake = _provider_with_fake_client()
    identity = RequestIdentity(tenant_id="anautics", actor_id="user-123")
    with bind_identity(identity):
        text = provider.converse(messages=[VisionMessage(text="hello")])
    assert text == "ok"
    (_path, payload, headers) = fake.calls[0]
    assert payload["user"] == "user-123"
    # Identity headers are what the proxy records as spend tags
    # (extra_spend_tag_headers); x-litellm-tags marks the call type.
    assert headers["x-captify-tenant-id"] == "anautics"
    assert headers["x-captify-actor-id"] == "user-123"
    assert headers["x-litellm-tags"] == "docling-vision,tenant:anautics,actor:user-123"


def test_provider_without_identity_sends_service_tags_only():
    provider, fake = _provider_with_fake_client()
    provider.converse(messages=[VisionMessage(text="hello")])
    (_path, payload, headers) = fake.calls[0]
    assert "user" not in payload
    assert headers["x-litellm-tags"] == "docling-vision"
