"""Per-request cost arithmetic.

A prompt cache is priced on both sides. The *read* is what the upstream reports
as served from the cache ('cached_tokens'); the *write* is what it reports as
created/stored ('cache_creation_input_tokens', the vLLM spelling
'created_cache_tokens', or the OpenAI client's 'cache_write_tokens'). A backend
that reports no creation count (SGLang, DeepSeek) is billed for those tokens at
the ordinary input rate, exactly as if no write rate were configured: Lumen
prices the cache accounting an upstream reports, and never infers a write count
from the remainder of the prompt.

The two rates are optional and independent: when a rate is unset, the matching
tokens are billed at the ordinary input rate, so an existing configuration
keeps its exact price.
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
    written to its prompt cache. A count the upstream did not report is None
    (or 0) and bills as ordinary input. When no cache rate is configured,
    preserve ordinary input pricing exactly. Zero is a valid configured rate.
    The two cache rates are independent: a rate that is unset bills its tokens
    at the input rate, so a model can price writes without pricing reads (and
    the other way round).
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
    """(prompt_tokens, cached_tokens, completion_tokens, write_tokens) from usage.

    The one place an upstream's usage object becomes numbers. Every billing and
    reporting path reads the same quadruple, so the streaming, non-streaming and
    audio routes cannot drift apart.

    Two usage layouts are normalized to the same total:

    * OpenAI style: 'prompt_tokens' is the whole prompt and already includes
      'prompt_tokens_details.cached_tokens' and '.cache_write_tokens'.
    * Anthropic style: 'input_tokens' is only the ordinary part, so the prompt
      is 'input_tokens' + 'cache_read_input_tokens' +
      'cache_creation_input_tokens'.

    Either way the first element is the full prompt, and the two cache counts
    split it rather than add to it. Each cache count is None when the upstream
    reports no such field at all and 0 when it reports zero; the difference is
    kept in 'request_logs', where NULL means "not reported" (and is billed at
    the input price) while a reported zero stays a zero. A count larger than
    the prompt (or than the remainder left for writes) is malformed and counts
    as unreported, so it can never discount or overcharge a request.
    """
    if usage is None:
        return 0, None, 0, None
    completion = _as_token_count(_field(usage, "completion_tokens"))
    if completion is None:
        completion = _as_token_count(_field(usage, "output_tokens")) or 0
    prompt = _as_token_count(_field(usage, "prompt_tokens"))
    if prompt is None:
        ordinary = _as_token_count(_field(usage, "input_tokens")) or 0
        cached = _as_token_count(_field(usage, "cache_read_input_tokens"))
        written = _as_token_count(_field(usage, "cache_creation_input_tokens"))
        if cached is not None or written is not None:
            # Anthropic style: input_tokens is the ordinary part only.
            return ordinary + (cached or 0) + (written or 0), cached, completion, written
        prompt = ordinary
    if not prompt:
        return prompt, None, completion, None
    details = _field(usage, "prompt_tokens_details")
    cached = _first_count((details, usage), _CACHE_READ_FIELDS, prompt)
    written = _first_count(
        (details, usage), _CACHE_WRITE_FIELDS, prompt - (cached or 0)
    )
    return prompt, cached, completion, written


#: Fields reporting the cached part of a prompt total (OpenAI style).
_CACHE_READ_FIELDS = ("cached_tokens",)

#: Fields reporting the written part of a prompt total. The first is the
#: OpenAI-compatible breakdown; the other two are the vLLM and OpenAI client
#: spellings of the same count.
_CACHE_WRITE_FIELDS = (
    "cache_creation_input_tokens",
    "created_cache_tokens",
    "cache_write_tokens",
)


def _first_count(holders, names, limit):
    """The first readable count no larger than limit found in holders, else None.

    Each holder (a usage object's nested details first, then the object itself)
    is searched for each name in turn, so a server that reports one spelling
    still prices correctly. A count above the limit cannot belong to this
    prompt, so it is skipped in favour of the next spelling.
    """
    for holder in holders:
        for name in names:
            count = _as_token_count(_field(holder, name))
            if count is not None and count <= limit:
                return count
    return None


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
