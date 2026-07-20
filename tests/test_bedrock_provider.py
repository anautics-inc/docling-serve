from docling_serve.providers.bedrock import (
    BedrockProvider,
    BedrockUnavailableError,
    VisionMessage,
)


def test_continuation_ends_with_user_turn(monkeypatch):
    provider = BedrockProvider(
        enabled=True,
        base_url="https://litellm.invalid",
        api_key="test-key",
        max_retries=1,
    )
    provider._client = object()
    payloads = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {"content": '{"components":['},
                        "finish_reason": "length",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {"content": "]}"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ]
    )

    def fake_post(client, payload, *, headers):
        payloads.append(payload.copy())
        return next(responses)

    monkeypatch.setattr(provider, "_post_chat_completion", fake_post)

    result = provider.converse(messages=[VisionMessage(text="Return JSON")])

    assert result == '{"components":[]}'
    assert payloads[0]["thinking"] == {"type": "adaptive"}
    assert payloads[0]["output_config"] == {"effort": "low"}
    assert "temperature" not in payloads[0]
    continuation = payloads[1]["messages"]
    assert continuation[-2]["role"] == "assistant"
    assert continuation[-1]["role"] == "user"
    assert "Continue exactly" in continuation[-1]["content"]


def test_empty_length_response_retries_with_larger_budget(monkeypatch):
    provider = BedrockProvider(
        enabled=True,
        base_url="https://litellm.invalid",
        api_key="test-key",
        max_tokens=1024,
        max_retries=1,
    )
    provider._client = object()
    budgets = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {"content": ""},
                        "finish_reason": "length",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {"content": '{"components": []}'},
                        "finish_reason": "stop",
                    }
                ]
            },
        ]
    )

    def fake_post(client, payload, *, headers):
        budgets.append(payload["max_tokens"])
        return next(responses)

    monkeypatch.setattr(provider, "_post_chat_completion", fake_post)

    result = provider.converse(messages=[VisionMessage(text="Return JSON")])

    assert result == '{"components": []}'
    assert budgets == [1024, 2048]


def test_reasoning_parameter_rejection_retries_without_thinking(monkeypatch):
    provider = BedrockProvider(
        enabled=True,
        base_url="https://litellm.invalid",
        api_key="test-key",
        max_retries=1,
    )
    provider._client = object()
    payloads = []

    def fake_post(client, payload, *, headers):
        payloads.append(payload.copy())
        if len(payloads) == 1:
            raise BedrockUnavailableError("thinking.type adaptive is not supported")
        return {
            "choices": [
                {
                    "message": {"content": '{"components": []}'},
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(provider, "_post_chat_completion", fake_post)

    result = provider.converse(messages=[VisionMessage(text="Return JSON")])

    assert result == '{"components": []}'
    assert "thinking" in payloads[0]
    assert "thinking" not in payloads[1]
    assert "output_config" not in payloads[1]
    assert payloads[1]["temperature"] == 0.0
