def calculate_cost(input_tokens: int, output_tokens: int, model_config) -> float:
    """Calculate cost in USD for a given token usage."""
    return round(
        input_tokens * float(model_config.input_cost_per_million) / 1_000_000
        + output_tokens * float(model_config.output_cost_per_million) / 1_000_000,
        6,
    )
