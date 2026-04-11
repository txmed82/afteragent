from __future__ import annotations

# Per-1000-token pricing in USD. Source: provider public pricing pages as of
# 2026-04. Update when providers change their rates.
#
# Format: (provider, model) -> (input_usd_per_1k, output_usd_per_1k)
#
# Ollama entries are omitted entirely — all Ollama costs are 0.
COST_PER_1K_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    # Anthropic
    ("anthropic", "claude-opus-4-6"): (0.015, 0.075),
    ("anthropic", "claude-sonnet-4-5"): (0.003, 0.015),
    ("anthropic", "claude-haiku-4-5"): (0.0008, 0.004),
    ("anthropic", "claude-3-5-sonnet-20241022"): (0.003, 0.015),
    ("anthropic", "claude-3-5-haiku-20241022"): (0.001, 0.005),

    # OpenAI
    ("openai", "gpt-4o"): (0.005, 0.015),
    ("openai", "gpt-4o-mini"): (0.00015, 0.0006),
    ("openai", "o1-preview"): (0.015, 0.060),
    ("openai", "o1-mini"): (0.003, 0.012),

    # OpenRouter — prices vary by underlying model. These are common aliases.
    ("openrouter", "anthropic/claude-3.5-sonnet"): (0.003, 0.015),
    ("openrouter", "anthropic/claude-3.5-haiku"): (0.001, 0.005),
    ("openrouter", "openai/gpt-4o"): (0.005, 0.015),
    ("openrouter", "openai/gpt-4o-mini"): (0.00015, 0.0006),
    ("openrouter", "meta-llama/llama-3.1-70b-instruct"): (0.00059, 0.00079),
    ("openrouter", "meta-llama/llama-3.1-8b-instruct"): (0.00018, 0.00018),
}


def estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Return the estimated USD cost for a single LLM call.

    Ollama always returns 0.0 (local inference). Unknown (provider, model)
    combinations also return 0.0 — we never guess.
    """
    if provider == "ollama":
        return 0.0
    rates = COST_PER_1K_TOKENS.get((provider, model))
    if rates is None:
        return 0.0
    input_rate, output_rate = rates
    return (input_tokens / 1000.0) * input_rate + (output_tokens / 1000.0) * output_rate
