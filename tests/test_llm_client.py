import sys
import types
from unittest.mock import MagicMock, patch

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


def _install_fake_anthropic_module(monkeypatch, mock_client):
    """Install a stub `anthropic` module with a minimal Anthropic class."""
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


def _install_fake_openai_module(monkeypatch, mock_client):
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "openai", fake_module)


def test_anthropic_client_forces_tool_use_and_parses_response(monkeypatch):
    fake_tool_use = MagicMock()
    fake_tool_use.type = "tool_use"
    fake_tool_use.input = {"findings": [{"code": "test_code"}]}

    fake_response = MagicMock()
    fake_response.content = [fake_tool_use]
    fake_response.usage.input_tokens = 1234
    fake_response.usage.output_tokens = 56

    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value = fake_response

    _install_fake_anthropic_module(monkeypatch, fake_anthropic)

    from afteragent.llm.anthropic_client import AnthropicClient

    cfg = LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key="sk-ant-test",
        base_url=None,
        max_tokens=4096,
        temperature=0.2,
    )
    client = AnthropicClient(cfg)

    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    response = client.call_structured(
        system="You are a diagnostician",
        user="Diagnose this run",
        schema=schema,
        tool_name="report_findings",
    )

    fake_anthropic.messages.create.assert_called_once()
    kwargs = fake_anthropic.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["max_tokens"] == 4096
    assert kwargs["temperature"] == 0.2
    assert kwargs["system"] == "You are a diagnostician"
    assert kwargs["messages"] == [{"role": "user", "content": "Diagnose this run"}]
    assert kwargs["tools"] == [
        {
            "name": "report_findings",
            "description": "Emit structured report_findings data.",
            "input_schema": schema,
        }
    ]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "report_findings"}

    assert response.data == {"findings": [{"code": "test_code"}]}
    assert response.input_tokens == 1234
    assert response.output_tokens == 56
    assert response.provider == "anthropic"
    assert response.model == "claude-sonnet-4-5"
    assert response.duration_ms >= 0


def test_openai_compat_client_uses_json_schema_response_format(monkeypatch):
    fake_completion = MagicMock()
    fake_completion.choices = [MagicMock()]
    fake_completion.choices[0].message.content = '{"findings": [{"code": "x"}]}'
    fake_completion.usage.prompt_tokens = 800
    fake_completion.usage.completion_tokens = 40

    fake_openai = MagicMock()
    fake_openai.chat.completions.create.return_value = fake_completion

    _install_fake_openai_module(monkeypatch, fake_openai)

    from afteragent.llm.openai_client import OpenAICompatClient

    cfg = LLMConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key="sk-oai-test",
        base_url=None,
        max_tokens=2048,
        temperature=0.1,
    )
    client = OpenAICompatClient(cfg)

    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    response = client.call_structured(
        system="System",
        user="User",
        schema=schema,
        tool_name="report_findings",
    )

    fake_openai.chat.completions.create.assert_called_once()
    kwargs = fake_openai.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["temperature"] == 0.1
    assert kwargs["messages"] == [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "User"},
    ]
    assert kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "report_findings",
            "schema": schema,
            "strict": True,
        },
    }

    assert response.data == {"findings": [{"code": "x"}]}
    assert response.input_tokens == 800
    assert response.output_tokens == 40
    assert response.provider == "openai"


def test_openai_compat_client_uses_base_url_for_openrouter(monkeypatch):
    fake_openai = MagicMock()
    fake_completion = MagicMock()
    fake_completion.choices = [MagicMock()]
    fake_completion.choices[0].message.content = '{"x": 1}'
    fake_completion.usage.prompt_tokens = 10
    fake_completion.usage.completion_tokens = 5
    fake_openai.chat.completions.create.return_value = fake_completion

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = MagicMock(return_value=fake_openai)
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    from afteragent.llm.openai_client import OpenAICompatClient

    cfg = LLMConfig(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        api_key="sk-or-test",
        base_url="https://openrouter.ai/api/v1",
    )
    OpenAICompatClient(cfg)

    # Verify the essential kwargs were passed, but don't be strict about
    # additional kwargs (e.g. timeout) that the client may also set.
    fake_module.OpenAI.assert_called_once()
    call_kwargs = fake_module.OpenAI.call_args.kwargs
    assert call_kwargs["api_key"] == "sk-or-test"
    assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"


def test_openai_compat_client_passes_placeholder_api_key_for_ollama(monkeypatch):
    fake_openai = MagicMock()
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = MagicMock(return_value=fake_openai)
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    from afteragent.llm.openai_client import OpenAICompatClient

    cfg = LLMConfig(
        provider="ollama",
        model="llama3.1:8b",
        api_key=None,
        base_url="http://localhost:11434/v1",
    )
    OpenAICompatClient(cfg)

    fake_module.OpenAI.assert_called_once()
    call_kwargs = fake_module.OpenAI.call_args.kwargs
    assert call_kwargs["api_key"] != ""
    assert call_kwargs["base_url"] == "http://localhost:11434/v1"


def test_anthropic_client_missing_tool_use_block_raises(monkeypatch):
    fake_text_block = MagicMock()
    fake_text_block.type = "text"

    fake_response = MagicMock()
    fake_response.content = [fake_text_block]
    fake_response.usage.input_tokens = 100
    fake_response.usage.output_tokens = 50

    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value = fake_response

    _install_fake_anthropic_module(monkeypatch, fake_anthropic)

    from afteragent.llm.anthropic_client import AnthropicClient

    cfg = LLMConfig(provider="anthropic", model="claude-sonnet-4-5", api_key="sk", base_url=None)
    client = AnthropicClient(cfg)

    with pytest.raises(ValueError, match="no tool_use block"):
        client.call_structured(
            system="S", user="U", schema={}, tool_name="report_findings",
        )
