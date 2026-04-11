from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..config import AppPaths

# Default model per provider when auto-detect fires.
_AUTODETECT_DEFAULTS = {
    "anthropic": ("claude-sonnet-4-5", None),
    "openai": ("gpt-4o-mini", None),
    "openrouter": ("anthropic/claude-3.5-sonnet", "https://openrouter.ai/api/v1"),
    "ollama": ("llama3.1:8b", "http://localhost:11434/v1"),
}

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"

_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


@dataclass(slots=True, frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout_s: float = 60.0
    auto_enhance_on_exec: bool = False


def load_config(
    paths: AppPaths,
    cli_overrides: dict | None = None,
) -> LLMConfig | None:
    """Walk the precedence chain (CLI → env → toml → auto-detect).

    Returns None if no provider is configured and no auto-detect branch hit,
    OR if a provider is configured but its API key is missing.
    """
    cli_overrides = cli_overrides or {}

    # Step 1: start with whatever the config file says (or an empty dict).
    file_data = _load_config_file(paths.config_path)

    # Step 2: merge env var overrides on top.
    env_provider = os.environ.get("AFTERAGENT_LLM_PROVIDER")
    env_model = os.environ.get("AFTERAGENT_LLM_MODEL")
    env_base_url = os.environ.get("AFTERAGENT_LLM_BASE_URL")

    provider = cli_overrides.get("provider") or env_provider or file_data.get("provider")
    model = cli_overrides.get("model") or env_model or file_data.get("model")
    base_url = cli_overrides.get("base_url") or env_base_url or file_data.get("base_url")
    auto_enhance_on_exec = bool(file_data.get("auto_enhance_on_exec", False))
    max_tokens = int(file_data.get("max_tokens", 4096))
    temperature = float(file_data.get("temperature", 0.2))
    timeout_s = float(file_data.get("timeout_s", 60.0))

    # Step 3: if provider is still not set, try auto-detect.
    if provider is None:
        provider, default_model, default_base_url = _autodetect()
        if provider is None:
            return None
        if model is None:
            model = default_model
        if base_url is None:
            base_url = default_base_url

    # Step 4: fill in a default base_url for providers that need one.
    if base_url is None and provider == "openrouter":
        base_url = _AUTODETECT_DEFAULTS["openrouter"][1]
    if provider == "ollama":
        base_url = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or _OLLAMA_DEFAULT_BASE_URL
        )
    if provider == "ollama" and os.environ.get("OLLAMA_BASE_URL"):
        base_url = os.environ["OLLAMA_BASE_URL"]

    # Step 5: resolve api_key. Ollama does not require one.
    api_key: str | None = None
    if provider in _API_KEY_ENV:
        api_key = os.environ.get(_API_KEY_ENV[provider])
        if api_key is None:
            return None

    if model is None:
        model = _AUTODETECT_DEFAULTS.get(provider, (None, None))[0]
        if model is None:
            return None

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
        auto_enhance_on_exec=auto_enhance_on_exec,
    )


def _load_config_file(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data.get("llm", {}) or {}


def _autodetect() -> tuple[str | None, str | None, str | None]:
    """Pick a provider based on which env vars are present.

    Priority: anthropic > openai > openrouter > ollama (reachable).
    Returns (provider, default_model, default_base_url) or (None, None, None).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        provider = "anthropic"
        model, base = _AUTODETECT_DEFAULTS[provider]
        return (provider, model, base)
    if os.environ.get("OPENAI_API_KEY"):
        provider = "openai"
        model, base = _AUTODETECT_DEFAULTS[provider]
        return (provider, model, base)
    if os.environ.get("OPENROUTER_API_KEY"):
        provider = "openrouter"
        model, base = _AUTODETECT_DEFAULTS[provider]
        return (provider, model, base)
    if os.environ.get("OLLAMA_BASE_URL"):
        provider = "ollama"
        model, base = _AUTODETECT_DEFAULTS[provider]
        return (provider, model, os.environ["OLLAMA_BASE_URL"])
    return (None, None, None)
