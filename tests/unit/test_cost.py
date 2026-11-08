from types import SimpleNamespace

import pytest

from lumen.services.cost import cached_tokens_from_usage, calculate_audio_cost, calculate_cost, usage_tokens, write_tokens_from_usage


def test_basic_cost():
    assert calculate_cost(1_000_000, 1_000_000, 1.0, 2.0) == 3.0


def test_zero_tokens():
    assert calculate_cost(0, 0, 1.0, 2.0) == 0.0


def test_only_input_tokens():
    assert calculate_cost(500_000, 0, 2.0, 0.0) == 1.0


def test_only_output_tokens():
    assert calculate_cost(0, 250_000, 0.0, 4.0) == 1.0


def test_cost_precision():
    result = calculate_cost(1, 1, 1.0, 1.0)
    assert result == round(2 / 1_000_000, 6)


def test_cost_rounded_to_six_decimals():
    result = calculate_cost(7, 7, 1.0, 1.0)
    assert len(str(result).split(".")[-1]) <= 6


def test_cached_input_cost_and_zero_rate():
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, 0.5) == 2.025
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, 0) == 1.9


def test_unset_cached_rate_preserves_ordinary_cost():
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000) == 2.4
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 2_000_000) == 2.4


@pytest.mark.parametrize("usage, expected", [
    (SimpleNamespace(prompt_tokens=100, prompt_tokens_details=SimpleNamespace(cached_tokens=40)), 40),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 35}}, 35),
    ({"prompt_tokens": 100, "cached_tokens": 25}, 25),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 110}}, 0),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1.5}}, 0),
    ({"prompt_tokens": 100, "cached_tokens": -1}, 0),
    (None, 0),
])
def test_cached_tokens_from_usage(usage, expected):
    assert cached_tokens_from_usage(usage) == expected


@pytest.mark.parametrize("usage, expected", [
    # Whole numbers echoed as floats or strings are read like the ints the
    # OpenAI client returns — OpenCode and the DeepSeek harness accept the same
    # shapes, so ignoring them would bill more than the client shows.
    ({"prompt_tokens": 100.0, "prompt_tokens_details": {"cached_tokens": 40.0}}, 40),
    ({"prompt_tokens": "100", "prompt_tokens_details": {"cached_tokens": "40"}}, 40),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": " 40 "}}, 40),
    # Anything else is not a count: reject rather than guess.
    ({"prompt_tokens": "abc", "prompt_tokens_details": {"cached_tokens": 40}}, 0),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": "1.5"}}, 0),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": True}}, 0),
    ({"prompt_tokens": True, "prompt_tokens_details": {"cached_tokens": 1}}, 0),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": float("nan")}}, 0),
    ({"prompt_tokens": 100, "prompt_tokens_details": object()}, 0),
])
def test_cached_tokens_from_usage_reads_the_wire_types_providers_send(usage, expected):
    assert cached_tokens_from_usage(usage) == expected


@pytest.mark.parametrize("usage, expected", [
    (None, (0, 0, 0, 0)),
    (SimpleNamespace(prompt_tokens=100, completion_tokens=7,
                     prompt_tokens_details=SimpleNamespace(cached_tokens=40)), (100, 40, 7, 60)),
    ({"prompt_tokens": 100.0, "completion_tokens": 7.0,
      "prompt_tokens_details": {"cached_tokens": 40.0}}, (100, 40, 7, 60)),
    # The audio upstream reports the same figures under these names.
    ({"type": "tokens", "input_tokens": "12", "output_tokens": "3"}, (12, 0, 3, 12)),
    # Unreadable counts become 0, so a prompt never carries a phantom cache.
    ({"prompt_tokens": "x", "completion_tokens": None}, (0, 0, 0, 0)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 140}}, (100, 0, 0, 100)),
    # vLLM 0.32: a warm repeat writes nothing and says so; the quadruple must
    # take the reported zero instead of the non-cached remainder.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 60, "created_cache_tokens": 0}}, (100, 60, 0, 0)),
])
def test_usage_tokens_normalizes_every_spelling(usage, expected):
    """One reader for every billing and reporting path: the same usage object
    must yield the same quadruple wherever it is read."""
    assert usage_tokens(usage) == expected


def test_calculate_cost_tolerates_an_unreadable_cached_count():
    """A count calculate_cost cannot read bills as ordinary input, never as a
    discount — the pre-caching behaviour — and never raises."""
    assert calculate_cost(1_000_000, 0, 2.0, 4.0, "not-a-count", 0.5) == 2.0
    assert calculate_cost(1_000_000, 0, 2.0, 4.0, None, 0.5) == 2.0
    assert calculate_cost(1_000_000, 0, 2.0, 4.0, "250000", 0.5) == 1.625


def test_cache_write_cost_and_its_clamps():
    # A write count within the remainder prices exactly, never overlapping
    # the cached part; a count above it clamps to the remainder.
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, 0.5, 500_000, 0.25) == 1.15
    assert calculate_cost(1_000_000, 0, 1.0, 2.0, 250_000, 0.5, 5_000_000, 0.25) == 0.3125
    # A cached count above the prompt total clamps to it, leaving no write
    # room at all.
    assert calculate_cost(10, 0, 1.0, 2.0, 5_000_000, 0.1, 7, 0.25) == 0.000001


def test_cache_write_rate_without_a_cached_rate():
    # The rates are independent: a model can price writes and leave reads at
    # the input price, and the unset rate must bill rather than raise.
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 0, None, 500_000, 0.25) == 1.525
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, None, 500_000, 0.25) == 1.525
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, None, 500_000, 0) == 1.4


def test_unset_cache_write_rate_bills_writes_at_the_input_rate():
    # Without a write rate the write portion stays in the input-rate block,
    # which is exactly how the request priced before write tokens existed.
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, 0.5, 500_000) == 2.025
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, 0.5, 500_000, None) == 2.025
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, 0.5) == 2.025


def test_no_cache_rates_is_the_legacy_formula():
    # With neither cache rate set the legacy formula prices the request
    # exactly, whatever counts (or unreadable counts) are reported.
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000, None, 500_000, None) == 2.4
    assert calculate_cost(1_000_000, 100_000, 2.0, 4.0, 250_000) == 2.4


@pytest.mark.parametrize("usage, prompt, cached, expected", [
    (None, 100, 0, 0),
    # No creation count reported: the non-cached remainder is the write.
    ({"prompt_tokens": 100, "completion_tokens": 7}, 100, 0, 100),
    # The OpenAI-compatible nested field, read like the cached_tokens twin.
    (SimpleNamespace(prompt_tokens=100, completion_tokens=7,
                     prompt_tokens_details=SimpleNamespace(cache_creation_input_tokens=40)), 100, 20, 40),
    # The top-level variant, and a whole number echoed as a string.
    ({"prompt_tokens": 100, "completion_tokens": 7, "cache_creation_input_tokens": 30}, 100, 20, 30),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": " 40 "}}, 100, 20, 40),
    # A count beyond the remainder clamps to it; a malformed one falls back
    # to the remainder as well, since either way those tokens were not
    # served from the cache.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": 90}}, 100, 20, 80),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": "x"}}, 100, 20, 80),
    # The same count under the spellings other servers use: vLLM's
    # created_cache_tokens (nested and top level) and the OpenAI client's
    # cache_write_tokens. vLLM sends null for the SDK field next to its own.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"created_cache_tokens": 40}}, 100, 20, 40),
    ({"prompt_tokens": 100, "created_cache_tokens": 10}, 100, 20, 10),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_write_tokens": 30}}, 100, 20, 30),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_write_tokens": None, "created_cache_tokens": 0}}, 100, 20, 0),
    # Anthropic's field wins when a server sends several.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": 25, "created_cache_tokens": 40}}, 100, 20, 25),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"created_cache_tokens": 90}}, 100, 20, 80),
    # A fully cached prompt leaves no write room.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": 40}}, 100, 100, 0),
])
def test_write_tokens_from_usage(usage, prompt, cached, expected):
    assert write_tokens_from_usage(usage, prompt, cached) == expected

def test_audio_cost_one_hour():
    assert calculate_audio_cost(3600, 0.10) == 0.10


def test_audio_cost_partial_hour():
    assert calculate_audio_cost(11, 0.6) == round(11 / 3600 * 0.6, 6)


def test_audio_cost_zero_rate():
    assert calculate_audio_cost(120, 0) == 0.0


def test_audio_cost_zero_seconds():
    assert calculate_audio_cost(0, 1.5) == 0.0
