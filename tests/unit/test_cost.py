from types import SimpleNamespace

import pytest

from lumen.services.cost import calculate_audio_cost, calculate_cost, usage_tokens


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


def test_an_unreported_cache_count_bills_as_ordinary_input():
    # None means the upstream said nothing about the cache; 0 means it said
    # zero. Either way the tokens bill at the input rate, but only the zero is
    # a report worth recording.
    assert calculate_cost(1_000_000, 0, 2.0, 4.0, None, 0.5, None, 0.25) == 2.0
    assert calculate_cost(1_000_000, 0, 2.0, 4.0, 0, 0.5, 0, 0.25) == 2.0


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


@pytest.mark.parametrize("usage, expected", [
    # Nothing to go on: the prompt is 0 and neither cache count was reported.
    (None, (0, None, 0, None)),
    # OpenAI style. 'prompt_tokens' is already the whole prompt; the cached and
    # written parts are inside it, not extra.
    (SimpleNamespace(prompt_tokens=100, completion_tokens=7,
                     prompt_tokens_details=SimpleNamespace(cached_tokens=40)),
     (100, 40, 7, None)),
    ({"prompt_tokens": 100.0, "completion_tokens": 7.0,
      "prompt_tokens_details": {"cached_tokens": 40.0}}, (100, 40, 7, None)),
    # The top-level spelling some servers use instead of the nested block.
    ({"prompt_tokens": 100, "cached_tokens": 25}, (100, 25, 0, None)),
    # A reported zero is a report: the backend does its own cache accounting
    # and this request read and wrote nothing.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0, "created_cache_tokens": 0}},
     (100, 0, 0, 0)),
    # No cache fields at all: neither count is reported, so nothing is
    # recorded for them (NULL in request_logs, billed at the input price).
    ({"prompt_tokens": 100, "completion_tokens": 7}, (100, None, 7, None)),
    # The audio upstream reports the same figures under these names.
    ({"type": "tokens", "input_tokens": "12", "output_tokens": "3"}, (12, None, 3, None)),
])
def test_usage_tokens_reads_openai_style_usage(usage, expected):
    assert usage_tokens(usage) == expected


@pytest.mark.parametrize("usage, expected", [
    # Anthropic style: 'input_tokens' is only the ordinary part, so the prompt
    # total is input + reads + writes. Normalized this way both styles report
    # the same three numbers for the same request.
    ({"input_tokens": 20, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 20,
      "output_tokens": 7}, (100, 60, 7, 20)),
    (SimpleNamespace(input_tokens=20, cache_read_input_tokens=60,
                     cache_creation_input_tokens=20, output_tokens=7), (100, 60, 7, 20)),
    # Only one side reported: the other stays unreported rather than 0.
    ({"input_tokens": 40, "cache_read_input_tokens": 60, "output_tokens": 7}, (100, 60, 7, None)),
    ({"input_tokens": 80, "cache_creation_input_tokens": 20}, (100, None, 0, 20)),
    # No cache fields: 'input_tokens' is the whole prompt.
    ({"input_tokens": 60, "output_tokens": 7}, (60, None, 7, None)),
    ({"input_tokens": "12", "output_tokens": "3"}, (12, None, 3, None)),
])
def test_usage_tokens_normalizes_anthropic_style_usage(usage, expected):
    assert usage_tokens(usage) == expected


@pytest.mark.parametrize("usage, expected", [
    # The write count, in every spelling an OpenAI-compatible server sends.
    # The first readable spelling wins, so a server that reports one of them
    # still prices correctly.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": 40}},
     (100, None, 0, 40)),
    ({"prompt_tokens": 100, "cache_creation_input_tokens": 30}, (100, None, 0, 30)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": " 40 "}},
     (100, None, 0, 40)),
    # vLLM's own spelling, and the OpenAI client's field next to it. vLLM
    # sends null for the SDK field, so the reported zero must win.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"created_cache_tokens": 40}}, (100, None, 0, 40)),
    ({"prompt_tokens": 100, "created_cache_tokens": 10}, (100, None, 0, 10)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_write_tokens": 30}}, (100, None, 0, 30)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_write_tokens": None, "created_cache_tokens": 0}},
     (100, None, 0, 0)),
    # Anthropic's field wins when a server sends several.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cache_creation_input_tokens": 25, "created_cache_tokens": 40}},
     (100, None, 0, 25)),
    # The cached part is split off first, so writes are limited to what is
    # left; a count past that did not come from this prompt.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 20, "created_cache_tokens": 80}},
     (100, 20, 0, 80)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 20, "created_cache_tokens": 90}},
     (100, 20, 0, None)),
    # A fully cached prompt leaves no write room.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 100,
                                                      "cache_creation_input_tokens": 40}},
     (100, 100, 0, None)),
])
def test_usage_tokens_reads_every_write_spelling(usage, expected):
    assert usage_tokens(usage) == expected


def test_usage_tokens_never_infers_a_write_count():
    """Only a reported creation count is a write. SGLang reports its cache
    reads and nothing about writes, so its non-cached remainder is ordinary
    input, billed at the input price — the same as a model with no write
    price configured."""
    sglang = {"prompt_tokens": 1513, "completion_tokens": 1,
              "prompt_tokens_details": {"cached_tokens": 1508}}
    assert usage_tokens(sglang) == (1513, 1508, 1, None)
    assert calculate_cost(1513, 1, 1.0, 2.0, 1508, 0.1, None, 0.05) == round(
        (1513 - 1508) * 1.0 / 1e6 + 1508 * 0.1 / 1e6 + 2.0 / 1e6, 6)


@pytest.mark.parametrize("usage, expected", [
    # A count the prompt cannot contain is malformed, not a discount: it
    # counts as unreported, and so does an unreadable one.
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 110}}, (100, None, 0, None)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1.5}}, (100, None, 0, None)),
    ({"prompt_tokens": 100, "cached_tokens": -1}, (100, None, 0, None)),
    ({"prompt_tokens": "abc", "completion_tokens": 3}, (0, None, 3, None)),
    ({"prompt_tokens": True, "completion_tokens": 3}, (0, None, 3, None)),
    ({"prompt_tokens": 100, "prompt_tokens_details": object()}, (100, None, 0, None)),
    ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": float("nan")}},
     (100, None, 0, None)),
])
def test_usage_tokens_rejects_counts_that_cannot_be_real(usage, expected):
    assert usage_tokens(usage) == expected


def test_audio_cost_one_hour():
    assert calculate_audio_cost(3600, 0.10) == 0.10


def test_audio_cost_partial_hour():
    assert calculate_audio_cost(11, 0.6) == round(11 / 3600 * 0.6, 6)


def test_audio_cost_zero_rate():
    assert calculate_audio_cost(120, 0) == 0.0


def test_audio_cost_zero_seconds():
    assert calculate_audio_cost(0, 1.5) == 0.0
