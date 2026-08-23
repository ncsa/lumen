def calculate_cost(input_tokens: int, output_tokens: int, input_cost_per_million, output_cost_per_million) -> float:
    """Calculate cost in USD for a given token usage.

    Takes the per-million rates as scalars (not a ModelConfig) because the
    streaming call sites extract them before releasing their DB session.
    """
    return round(
        input_tokens * float(input_cost_per_million) / 1_000_000
        + output_tokens * float(output_cost_per_million) / 1_000_000,
        6,
    )


def calculate_audio_cost(seconds: float, cost_per_hour) -> float:
    """Calculate cost in USD for a given duration of audio (speech-to-text)."""
    return round(seconds / 3600 * float(cost_per_hour), 6)
