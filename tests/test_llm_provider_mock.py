from __future__ import annotations

import json

import pytest

from mirza_analyzer.llm_providers import (
    LLMProviderError,
    LMStudioProvider,
    MockProvider,
    build_provider,
)


def test_mock_provider_records_calls_and_returns_text() -> None:
    def handler(system: str, user: str, model: str) -> str:
        return json.dumps({"echo": user[:20], "model": model})

    provider = MockProvider(handler=handler)
    response = provider.chat(
        system_prompt="sys",
        user_prompt="hello world",
        model="qwen3.6-27b",
        temperature=0.0,
        timeout_seconds=5.0,
    )

    assert provider.name == "mock"
    assert provider.calls and provider.calls[0]["model"] == "qwen3.6-27b"
    payload = json.loads(response.text)
    assert payload["model"] == "qwen3.6-27b"
    assert payload["echo"].startswith("hello")


def test_mock_provider_default_handler_returns_valid_json() -> None:
    provider = MockProvider()
    response = provider.chat(
        system_prompt="",
        user_prompt="",
        model="",
        temperature=0.0,
        timeout_seconds=5.0,
    )
    payload = json.loads(response.text)
    assert payload["decision"] == "needs_human"
    assert payload["confidence"] == "low"


def test_build_provider_unknown_provider_raises() -> None:
    with pytest.raises(ValueError):
        build_provider("openrouter", base_url="http://x")


def test_lmstudio_provider_raises_clear_error_when_unreachable() -> None:
    provider = LMStudioProvider(base_url="http://127.0.0.1:1/v1")
    with pytest.raises(LLMProviderError) as excinfo:
        provider.chat(
            system_prompt="sys",
            user_prompt="user",
            model="any",
            temperature=0.0,
            timeout_seconds=1.0,
        )
    message = str(excinfo.value)
    assert "127.0.0.1" in message or "LM Studio" in message
