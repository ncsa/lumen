from types import SimpleNamespace

from lumen.services.cost import calculate_cost


def make_config(input_cost, output_cost):
    return SimpleNamespace(
        input_cost_per_million=input_cost,
        output_cost_per_million=output_cost,
    )


def test_basic_cost():
    cfg = make_config(1.0, 2.0)
    result = calculate_cost(1_000_000, 1_000_000, cfg)
    assert result == 3.0


def test_zero_tokens():
    cfg = make_config(1.0, 2.0)
    assert calculate_cost(0, 0, cfg) == 0.0


def test_only_input_tokens():
    cfg = make_config(2.0, 0.0)
    assert calculate_cost(500_000, 0, cfg) == 1.0


def test_only_output_tokens():
    cfg = make_config(0.0, 4.0)
    assert calculate_cost(0, 250_000, cfg) == 1.0


def test_cost_precision():
    cfg = make_config(1.0, 1.0)
    result = calculate_cost(1, 1, cfg)
    assert result == round(2 / 1_000_000, 6)


def test_cost_rounded_to_six_decimals():
    cfg = make_config(1.0, 1.0)
    result = calculate_cost(7, 7, cfg)
    assert len(str(result).split(".")[-1]) <= 6
