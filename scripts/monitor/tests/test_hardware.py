import pytest

from monitor.config import Gpu
from monitor.hardware import daily_cost

RTX6000 = Gpu(energy=600, price=625)


def test_one_healthy_rtx6000():
    # 625 × 12/365 + 0.6 kW × 24 h × 0.24 $/kWh = 20.548 + 3.456
    assert daily_cost(RTX6000, 1, 0.24, 1.0) == pytest.approx(24.01, abs=0.01)


def test_two_gpus_double_the_cost():
    assert daily_cost(RTX6000, 2, 0.24, 1.0) == pytest.approx(2 * daily_cost(RTX6000, 1, 0.24, 1.0))


def test_scales_with_average_healthy():
    assert daily_cost(RTX6000, 1, 0.24, 0.5) == pytest.approx(daily_cost(RTX6000, 1, 0.24, 1.0) / 2)
    assert daily_cost(RTX6000, 1, 0.24, 0) == 0
