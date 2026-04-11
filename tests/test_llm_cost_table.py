from afteragent.llm.cost_table import estimate_cost


def test_estimate_cost_anthropic_sonnet_4_5():
    cost = estimate_cost("anthropic", "claude-sonnet-4-5", 1_000_000, 500_000)
    assert abs(cost - (3.0 + 7.5)) < 0.01


def test_estimate_cost_anthropic_haiku_4_5_is_cheaper_than_sonnet():
    sonnet = estimate_cost("anthropic", "claude-sonnet-4-5", 100_000, 20_000)
    haiku = estimate_cost("anthropic", "claude-haiku-4-5", 100_000, 20_000)
    assert haiku < sonnet
    assert haiku > 0


def test_estimate_cost_openai_gpt_4o_mini():
    cost = estimate_cost("openai", "gpt-4o-mini", 100_000, 20_000)
    assert cost > 0
    assert cost < 1.0


def test_estimate_cost_ollama_is_always_zero():
    cost = estimate_cost("ollama", "llama3.1:8b", 1_000_000, 1_000_000)
    assert cost == 0.0


def test_estimate_cost_ollama_with_unknown_model_is_still_zero():
    cost = estimate_cost("ollama", "some-custom-tune:v2", 100, 100)
    assert cost == 0.0


def test_estimate_cost_unknown_provider_returns_zero():
    cost = estimate_cost("made-up", "model-x", 100_000, 20_000)
    assert cost == 0.0


def test_estimate_cost_unknown_model_on_known_provider_returns_zero():
    cost = estimate_cost("anthropic", "claude-future-model-9", 100_000, 20_000)
    assert cost == 0.0


def test_estimate_cost_scales_linearly_with_tokens():
    base = estimate_cost("anthropic", "claude-sonnet-4-5", 10_000, 5_000)
    doubled = estimate_cost("anthropic", "claude-sonnet-4-5", 20_000, 10_000)
    assert abs(doubled - 2 * base) < 1e-6
