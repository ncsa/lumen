from lumen.services.cost import calculate_audio_cost, calculate_cost


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


def test_audio_cost_one_hour():
    assert calculate_audio_cost(3600, 0.10) == 0.10


def test_audio_cost_partial_hour():
    assert calculate_audio_cost(11, 0.6) == round(11 / 3600 * 0.6, 6)


def test_audio_cost_zero_rate():
    assert calculate_audio_cost(120, 0) == 0.0


def test_audio_cost_zero_seconds():
    assert calculate_audio_cost(0, 1.5) == 0.0
