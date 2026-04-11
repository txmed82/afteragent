from __future__ import annotations

import time

from .client import StructuredResponse
from .config import LLMConfig


class AnthropicClient:
    """LLMClient implementation using the Anthropic Messages API.

    Uses tool_choice={"type": "tool", "name": ...} to force Claude to return
    exactly one tool_use block with input matching the provided schema.
    """

    name = "anthropic"

    def __init__(self, config: LLMConfig):
        import anthropic  # Lazy import — only happens when this class is instantiated.

        self._config = config
        self._sdk = anthropic.Anthropic(api_key=config.api_key, timeout=config.timeout_s)
        self.model = config.model

    def call_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
    ) -> StructuredResponse:
        start = time.time()
        response = self._sdk.messages.create(
            model=self.model,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            timeout=self._config.timeout_s,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": tool_name,
                    "description": f"Emit structured {tool_name} data.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )

        tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not tool_use_blocks:
            raise ValueError(
                f"Anthropic response contained no tool_use block for tool {tool_name!r}. "
                f"Got content types: {[getattr(b, 'type', '?') for b in response.content]}"
            )

        data = tool_use_blocks[0].input
        return StructuredResponse(
            data=dict(data),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=self.model,
            provider="anthropic",
            duration_ms=int((time.time() - start) * 1000),
            raw_response_excerpt=str(data)[:500],
        )