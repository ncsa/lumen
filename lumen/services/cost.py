def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    input_cost_per_million,
    output_cost_per_million,
    cached_input_tokens: int = 0,
    cached_input_cost_per_million=None,
) -> float:
    """Calculate cost in USD for a given token usage.

    Takes the per-million rates as scalars (not a ModelConfig) because the
    streaming call sites extract them before releasing their DB session.

    ``cached_input_tokens`` is a subset of ``input_tokens`` reported by the
    upstream. When no cached-input rate is configured, preserve ordinary input
    pricing exactly. Zero is a valid configured rate.
    """
    if cached_input_cost_per_million is None:
        return round(
            input_tokens * float(input_cost_per_million) / 1_000_000
            + output_tokens * float(output_cost_per_million) / 1_000_000,
            6,
        )
    cached = min(max(cached_input_tokens, 0), input_tokens)
    return round(
        (input_tokens - cached) * float(input_cost_per_million) / 1_000_000
        + cached * float(cached_input_cost_per_million) / 1_000_000
        + output_tokens * float(output_cost_per_million) / 1_000_000,
        6,
    )


def cached_tokens_from_usage(usage) -> int:
    """The prompt tokens ``usage`` says were served from a cache, else 0.

    Reads the OpenAI-compatible ``prompt_tokens_details.cached_tokens`` field,
    or the top-level ``cached_tokens`` variant. Only a nonnegative integer
    within the reported prompt total is eligible for discounted pricing.
    """
    if usage is None:
        return 0
    prompt_tokens = _field(usage, "prompt_tokens")
    if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        return 0
    details = _field(usage, "prompt_tokens_details")
    for value in (_field(details, "cached_tokens"), _field(usage, "cached_tokens")):
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= prompt_tokens:
            return value
    return 0


def _field(holder, name: str):
    """``holder.name`` from a pydantic model, a plain object or a mapping."""
    if isinstance(holder, dict):
        return holder.get(name)
    return getattr(holder, name, None)


def calculate_audio_cost(seconds: float, cost_per_hour) -> float:
    """Calculate cost in USD for a given duration of audio (speech-to-text)."""
    return round(seconds / 3600 * float(cost_per_hour), 6)
