import tempfile
from pathlib import Path

import pytest

from afteragent.config import resolve_paths
from afteragent.llm.config import LLMConfig, load_config


def _paths(tmp: Path):
    return resolve_paths(tmp)


def test_returns_none_when_nothing_configured(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is None


def test_autodetect_anthropic_when_only_anthropic_key_set(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-sonnet-4-5"
    assert cfg.api_key == "sk-ant-test"
    assert cfg.auto_enhance_on_exec is False


def test_autodetect_openai_when_only_openai_key_set(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.api_key == "sk-oai-test"


def test_autodetect_openrouter_when_only_openrouter_key_set(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "openrouter"
    assert cfg.model == "anthropic/claude-3.5-sonnet"
    assert cfg.api_key == "sk-or-test"
    assert cfg.base_url == "https://openrouter.ai/api/v1"


def test_anthropic_precedence_over_openai_in_autodetect(tmp_path, monkeypatch):
    for var in [
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "anthropic"


def test_config_file_overrides_autodetect(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-opus-4-6"\n'
        'auto_enhance_on_exec = true\n'
        'max_tokens = 8192\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-opus-4-6"
    assert cfg.auto_enhance_on_exec is True
    assert cfg.max_tokens == 8192


def test_env_var_overrides_config_file(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("AFTERAGENT_LLM_MODEL", "claude-haiku-4-5")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-opus-4-6"\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.model == "claude-haiku-4-5"


def test_cli_overrides_win_over_env_and_config(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("AFTERAGENT_LLM_MODEL", "claude-haiku-4-5")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-opus-4-6"\n'
    )

    cfg = load_config(paths, cli_overrides={"model": "claude-sonnet-4-5"})
    assert cfg is not None
    assert cfg.model == "claude-sonnet-4-5"


def test_ollama_needs_no_api_key(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "ollama"\n'
        'model = "qwen2.5-coder:7b"\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "ollama"
    assert cfg.api_key is None
    assert cfg.base_url == "http://localhost:11434/v1"


def test_ollama_base_url_override_from_env(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://remote-ollama:11434/v1")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "ollama"\n'
        'model = "qwen2.5-coder:7b"\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.base_url == "http://remote-ollama:11434/v1"


def test_missing_api_key_for_configured_provider_returns_none(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-sonnet-4-5"\n'
    )

    cfg = load_config(paths)
    assert cfg is None
