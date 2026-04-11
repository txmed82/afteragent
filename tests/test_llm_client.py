from unittest.mock import patch

import pytest

from afteragent.llm.client import StructuredResponse, get_client
from afteragent.llm.config import LLMConfig


def _make_config(provider: str, api_key: str = "fake-key") -> LLMConfig:
    return LLMConfig(
        provider=provider,
        model="model-x",
        api_key=api_key if provider != "ollama" else None,
        base_url="http://localhost:11434/v1" if provider == "ollama" else None,
    )


def test_structured_response_dataclass_shape():
    r = StructuredResponse(
        data={"findings": []},
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-5",
        provider="anthropic",
        duration_ms=1200,
        raw_response_excerpt='{"findings": []}',
    )
    assert r.data == {"findings": []}
    assert r.input_tokens == 100
    assert r.output_tokens == 50
    assert r.provider == "anthropic"


def test_get_client_dispatches_anthropic_to_anthropic_client():
    cfg = _make_config("anthropic")
    with patch("afteragent.llm.client._build_anthropic_client") as build:
        build.return_value = object()
        client = get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_dispatches_openai_to_openai_compat_client():
    cfg = _make_config("openai")
    with patch("afteragent.llm.client._build_openai_compat_client") as build:
        build.return_value = object()
        get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_dispatches_openrouter_to_openai_compat_client():
    cfg = _make_config("openrouter")
    with patch("afteragent.llm.client._build_openai_compat_client") as build:
        build.return_value = object()
        get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_dispatches_ollama_to_openai_compat_client():
    cfg = _make_config("ollama")
    with patch("afteragent.llm.client._build_openai_compat_client") as build:
        build.return_value = object()
        get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_unknown_provider_raises():
    cfg = LLMConfig(provider="unknown", model="x", api_key="k", base_url=None)
    with pytest.raises(ValueError, match="Unknown provider"):
        get_client(cfg)


def test_get_client_missing_anthropic_sdk_raises_clear_error():
    cfg = _make_config("anthropic")
    with patch(
        "afteragent.llm.client._build_anthropic_client",
        side_effect=ImportError("No module named 'anthropic'"),
    ):
        with pytest.raises(ImportError, match="afteragent\\[anthropic\\]"):
            get_client(cfg)


def test_get_client_missing_openai_sdk_raises_clear_error():
    cfg = _make_config("openai")
    with patch(
        "afteragent.llm.client._build_openai_compat_client",
        side_effect=ImportError("No module named 'openai'"),
    ):
        with pytest.raises(ImportError, match="afteragent\\[openai\\]"):
            get_client(cfg)
