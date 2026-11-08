"""Per-request cost arithmetic.

A prompt cache is priced on both sides. The *read* is what the upstream reports
as served from the cache ('cached_tokens'); the *write* is what it reports as
created/stored ('cache_creation_input_tokens', the vLLM spelling
'created_cache_tokens', or the OpenAI client's 'cache_write_tokens'), falling
back to the non-cached remainder of the prompt when no creation count is
reported (SGLang and DeepSeek report none, yet still process and store those
tokens). The two rates are optional and independent: when a rate is unset, the
matching tokens are billed at the ordinary input rate, so an existing
configuration keeps its exact price.
"""


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    input_cost_per_million,
    output_cost_per_million,
    cached_input_tokens: int = 0,
    cached_input_cost_per_million=None,
    cache_write_tokens: int = 0,
    cache_write_cost_per_million=None,
) -> float:
    """Calculate cost in USD for a given token usage.

    Takes the per-million rates as scalars (not a ModelConfig) because the
    streaming call sites extract them before releasing their DB session.

    'cached_input_tokens' is a subset of 'input_tokens' reported by the
    upstream; 'cache_write_tokens' is the part of the remainder it reports as
    written to its prompt cache (or the whole remainder when no count is
    reported). When no cache rate is configured, preserve ordinary input
    pricing exactly. Zero is a valid configured rate. The two cache rates are
    independent: a rate that is unset bills its tokens at the input rate, so a
    model can price writes without pricing reads (and the other way round).
    """
    if cached_input_cost_per_million is None and cache_write_cost_per_million is None:
        return round(
            input_tokens * float(input_cost_per_million) / 1_000_000
            + output_tokens * float(output_cost_per_million) / 1_000_000,
            6,
        )
    # An unreadable count is billed as ordinary input, never as a discount, and
    # a count above the reported prompt total cannot credit the request. Write
    # tokens are capped at the remainder after the cached part, so the two
    # breakdowns never overlap or exceed the prompt total.
    cached = min(_as_token_count(cached_input_tokens) or 0, input_tokens)
    write = min(_as_token_count(cache_write_tokens) or 0, input_tokens - cached)
    input_rate = float(input_cost_per_million)
    output_rate = float(output_cost_per_million)
    # The rates are independent, so the unset one bills its tokens at the
    # ordinary input rate instead of leaving a None to be multiplied.
    cached_rate = (
        input_rate
        if cached_input_cost_per_million is None
        else float(cached_input_cost_per_million)
    )
    if cache_write_cost_per_million is None:
        # No write rate: the write portion stays in the input-rate block, which
        # is exactly how this request priced before write tokens existed.
        return round(
            (input_tokens - cached) * input_rate / 1_000_000
            + cached * cached_rate / 1_000_000
            + output_tokens * output_rate / 1_000_000,
            6,
        )
    write_rate = float(cache_write_cost_per_million)
    return round(
        (input_tokens - cached - write) * input_rate / 1_000_000
        + cached * cached_rate / 1_000_000
        + write * write_rate / 1_000_000
        + output_tokens * output_rate / 1_000_000,
        6,
    )


def usage_tokens(usage) -> tuple:
    """(prompt_tokens, cached_prompt_tokens, completion_tokens, write_tokens) from usage.

    The one place an upstream's usage object becomes numbers. Every billing and
    reporting path reads the same quadruple, so the streaming, non-streaming and
    audio routes cannot drift apart. A count Lumen cannot read becomes 0 for the
    read and the non-cached remainder for the write; neither ever discounts the
    request below the input-price line.

    'prompt_tokens'/'completion_tokens' are the OpenAI spellings;
    'input_tokens'/'output_tokens' cover the audio upstream, which reports
    the same figures under those names.
    """
    if usage is None:
        return 0, 0, 0, 0
    prompt = _as_token_count(_field(usage, "prompt_tokens"))
    if prompt is None:
        prompt = _as_token_count(_field(usage, "input_tokens")) or 0
    completion = _as_token_count(_field(usage, "completion_tokens"))
    if completion is None:
        completion = _as_token_count(_field(usage, "output_tokens")) or 0
    # cached_tokens_from_usage re-derives the prompt total; a prompt of 0 can
    # have no cached part, so skip it and never report a cache on no input.
    if not prompt:
        return prompt, 0, completion, 0
    cached = cached_tokens_from_usage(usage)
    return prompt, cached, completion, write_tokens_from_usage(usage, prompt, cached)


def cached_tokens_from_usage(usage) -> int:
    """The prompt tokens usage says were served from a cache, else 0.

    Reads the OpenAI-compatible 'prompt_tokens_details.cached_tokens' field,
    or the top-level 'cached_tokens' variant. Only a count within the reported
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


def write_tokens_from_usage(usage, prompt_tokens: int = 0, cached_tokens: int = 0) -> int:
    """The prompt tokens usage says were written into a cache, else the remainder.

    Reads the OpenAI-compatible 'prompt_tokens_details.cache_creation_input_tokens'
    field (the counterpart of 'cached_tokens', reported by Anthropic and its
    compatibles), then the two spellings other servers use for the same count:
    vLLM's 'created_cache_tokens' and the OpenAI client's 'cache_write_tokens'.
    Each is read nested first, then at the top level. Backends that never report
    a creation count (SGLang, DeepSeek) still have to process and store the
    non-cached part of the prompt, so the count falls back to the non-cached
    remainder — that is what a write price pays for. Only a count within the
    remainder is eligible, and a malformed count falls back to the remainder as
    well, since either way those tokens cannot have been served from the cache.
    """
    if usage is None:
        return 0
    remainder = (prompt_tokens or 0) - (cached_tokens or 0)
    if remainder <= 0:
        return 0
    details = _field(usage, "prompt_tokens_details")
    for value in (
        _field(details, "cache_creation_input_tokens"),
        _field(details, "created_cache_tokens"),
        _field(details, "cache_write_tokens"),
        _field(usage, "cache_creation_input_tokens"),
        _field(usage, "created_cache_tokens"),
        _field(usage, "cache_write_tokens"),
    ):
        written = _as_token_count(value)
        if written is not None and written <= remainder:
            return written
    return remainder


def _as_token_count(value):
    """value as a nonnegative int, else None.

    Usage counters reach here from more than one backend implementation. The
    OpenAI client hands back ints, but an OpenAI-compatible server may echo a
    whole number as a float (1024.0) or as a string, and clients such as
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
    """holder.name from a pydantic model, a plain object or a mapping."""
    if isinstance(holder, dict):
        return holder.get(name)
    return getattr(holder, name, None)


def calculate_audio_cost(seconds: float, cost_per_hour) -> float:
    """Calculate cost in USD for a given duration of audio (speech-to-text)."""
    return round(seconds / 3600 * float(cost_per_hour), 6)
