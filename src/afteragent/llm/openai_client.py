from __future__ import annotations

import json
import time

from .client import StructuredResponse
from .config import LLMConfig

# Ollama does not enforce an API key, but the `openai` SDK requires some
# string. Use a visible placeholder so it's obvious in debugging.
_OLLAMA_PLACEHOLDER_KEY = "ollama-no-auth"


class OpenAICompatClient:
    """LLMClient implementation using the OpenAI-compatible Chat Completions
    API. Works unchanged for OpenAI, OpenRouter, and Ollama by varying
    base_url and api_key.
    """

    name = "openai-compat"

    def __init__(self, config: LLMConfig):
        import openai  # Lazy import.

        self._config = config
        self.model = config.model

        api_key = config.api_key or _OLLAMA_PLACEHOLDER_KEY
        if config.base_url is not None:
            self._sdk = openai.OpenAI(api_key=api_key, base_url=config.base_url, timeout=config.timeout_s)
        else:
            self._sdk = openai.OpenAI(api_key=api_key, timeout=config.timeout_s)

    def call_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
    ) -> StructuredResponse:
        start = time.time()
        response = self._sdk.chat.completions.create(
            model=self.model,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": tool_name,
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        usage = response.usage
        return StructuredResponse(
            data=data,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            model=self.model,
            provider=self._config.provider,
            duration_ms=int((time.time() - start) * 1000),
            raw_response_excerpt=content[:500],
        )