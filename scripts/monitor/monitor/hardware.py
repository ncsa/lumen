"""Hardware and energy cost of running a model for a day."""

from monitor.config import Gpu

DAYS_PER_MONTH = 365 / 12


def gpu_day_cost(gpu: Gpu, kwh: float) -> float:
    """$ for one GPU running all day: its monthly price plus its rated power at `kwh`."""
    return gpu.price / DAYS_PER_MONTH + gpu.energy / 1000 * 24 * kwh


def daily_cost(gpu: Gpu, gpus: int, kwh: float, avg_healthy: float) -> float:
    """`avg_healthy × gpus × (price × 12/365 + energy/1000 × 24 × kwh)`."""
    return avg_healthy * gpus * gpu_day_cost(gpu, kwh)
