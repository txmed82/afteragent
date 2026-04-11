from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import LLMConfig


@dataclass(slots=True)
class StructuredResponse:
    data: dict
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    duration_ms: int
    raw_response_excerpt: str


class LLMClient(Protocol):
    """Runtime-dispatched LLM client. Both implementations return the same
    StructuredResponse shape so callers never see provider-specific types."""

    name: str
    model: str

    def call_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
    ) -> StructuredResponse: ...


def get_client(config: LLMConfig) -> LLMClient:
    """Factory: pick the right client implementation for the configured provider.

    Uses lazy imports so users who installed `afteragent[anthropic]` but not
    `afteragent[openai]` don't fail at import time — only at instantiation
    time, and only when they try to use the missing provider.
    """
    if config.provider == "anthropic":
        try:
            return _build_anthropic_client(config)
        except ImportError as exc:
            raise ImportError(
                f"Provider 'anthropic' requires `pip install afteragent[anthropic]`. "
                f"Underlying error: {exc}"
            ) from exc
    if config.provider in ("openai", "openrouter", "ollama"):
        try:
            return _build_openai_compat_client(config)
        except ImportError as exc:
            raise ImportError(
                f"Provider '{config.provider}' requires `pip install afteragent[openai]`. "
                f"Underlying error: {exc}"
            ) from exc
    raise ValueError(f"Unknown provider: {config.provider}")


def _build_anthropic_client(config: LLMConfig) -> LLMClient:
    """Lazy import + construction of the Anthropic client."""
    from .anthropic_client import AnthropicClient
    return AnthropicClient(config)


def _build_openai_compat_client(config: LLMConfig) -> LLMClient:
    """Lazy import + construction of the OpenAI-compatible client."""
    from .openai_client import OpenAICompatClient
    return OpenAICompatClient(config)
