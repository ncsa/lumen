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
    # An unreadable count is billed as ordinary input, never as a discount, and
    # a count above the reported prompt total cannot credit the request.
    cached = min(_as_token_count(cached_input_tokens) or 0, input_tokens)
    return round(
        (input_tokens - cached) * float(input_cost_per_million) / 1_000_000
        + cached * float(cached_input_cost_per_million) / 1_000_000
        + output_tokens * float(output_cost_per_million) / 1_000_000,
        6,
    )


def cached_tokens_from_usage(usage) -> int:
    """The prompt tokens ``usage`` says were served from a cache, else 0.

    Reads the OpenAI-compatible ``prompt_tokens_details.cached_tokens`` field,
    or the top-level ``cached_tokens`` variant. Only a count within the reported
    prompt total is eligible for discounted pricing, and an unreadable count is
    ignored rather than guessed at.
    """
    if usage is None:
        return 0
    prompt_tokens = _as_token_count(_field(usage, "prompt_tokens"))
    if prompt_tokens is None:
        return 0
    details = _field(usage, "prompt_tokens_details")
    for value in (_field(details, "cached_tokens"), _field(usage, "cached_tokens")):
        cached = _as_token_count(value)
        if cached is not None and cached <= prompt_tokens:
            return cached
    return 0


def _as_token_count(value):
    """``value`` as a nonnegative int, else None.

    Usage counters reach here from more than one backend implementation. The
    OpenAI client hands back ints, but an OpenAI-compatible server may echo a
    whole number as a float (``1024.0``) or as a string, and clients such as
    OpenCode and the DeepSeek harness accept any of those before pricing the
    cache read themselves. Reading them the same way keeps Lumen's bill equal to
    the count the client shows. Bools, fractions, negatives and NaN/inf are
    rejected: a malformed count must never move money.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _field(holder, name: str):
    """``holder.name`` from a pydantic model, a plain object or a mapping."""
    if isinstance(holder, dict):
        return holder.get(name)
    return getattr(holder, name, None)


def calculate_audio_cost(seconds: float, cost_per_hour) -> float:
    """Calculate cost in USD for a given duration of audio (speech-to-text)."""
    return round(seconds / 3600 * float(cost_per_hour), 6)
