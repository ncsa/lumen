def calculate_cost(input_tokens: int, output_tokens: int, model_config) -> float:
    """Calculate cost in USD for a given token usage."""
    return round(
        input_tokens * float(model_config.input_cost_per_million) / 1_000_000
        + output_tokens * float(model_config.output_cost_per_million) / 1_000_000,
        6,
    )


def calculate_audio_cost(seconds: float, cost_per_hour) -> float:
    """Calculate cost in USD for a given duration of audio (speech-to-text)."""
    return round(seconds / 3600 * float(cost_per_hour), 6)
