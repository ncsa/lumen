from types import SimpleNamespace

import pytest

from lumen.services.cost import cached_tokens_from_usage, calculate_audio_cost, calculate_cost


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


def test_audio_cost_one_hour():
    assert calculate_audio_cost(3600, 0.10) == 0.10


def test_audio_cost_partial_hour():
    assert calculate_audio_cost(11, 0.6) == round(11 / 3600 * 0.6, 6)


def test_audio_cost_zero_rate():
    assert calculate_audio_cost(120, 0) == 0.0


def test_audio_cost_zero_seconds():
    assert calculate_audio_cost(0, 1.5) == 0.0
