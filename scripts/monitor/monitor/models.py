"""Database tables.

Timestamps are naive UTC; only `daily_stats.day` is a local date (the
configured timezone decides where a day starts).
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from monitor.db import utcnow

SOURCES = ("models.dev", "lumen", "hardware")


def token_sum(input_tokens, output_tokens):
    """Input + output tokens, or None when either is unknown."""
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


class Base(DeclarativeBase):
    pass


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (
        UniqueConstraint("day", "model", "source", name="uq_daily_stats_day_model_source"),
        CheckConstraint("source IN ('models.dev', 'lumen', 'hardware')", name="ck_daily_stats_source"),
        {"comment": "One row per local day, Lumen model and price source; kept forever"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, comment="Local date in the configured timezone")
    model: Mapped[str] = mapped_column(String(255), nullable=False, comment="Lumen model id")
    source: Mapped[str] = mapped_column(String(16), nullable=False, comment="models.dev, lumen or hardware")
    last_updated: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="Last write (naive UTC)")

    # source = models.dev: provider prices in $ per million tokens, $0 listings excluded
    modelsdev_id: Mapped[Optional[str]] = mapped_column(String(255), comment="Matched models.dev model id")
    input_median: Mapped[Optional[float]] = mapped_column(Float, comment="Median provider input price, $/M tokens")
    input_p5: Mapped[Optional[float]] = mapped_column(Float, comment="5th percentile provider input price, $/M tokens")
    input_p95: Mapped[Optional[float]] = mapped_column(Float, comment="95th percentile provider input price, $/M tokens")
    output_median: Mapped[Optional[float]] = mapped_column(Float, comment="Median provider output price, $/M tokens")
    output_p5: Mapped[Optional[float]] = mapped_column(Float, comment="5th percentile provider output price, $/M tokens")
    output_p95: Mapped[Optional[float]] = mapped_column(Float, comment="95th percentile provider output price, $/M tokens")
    providers: Mapped[Optional[int]] = mapped_column(Integer, comment="Number of non-zero provider listings")

    # source = lumen: what Lumen charges, its traffic and availability for the day
    input_price: Mapped[Optional[float]] = mapped_column(Float, comment="Lumen input price, $/M tokens")
    output_price: Mapped[Optional[float]] = mapped_column(Float, comment="Lumen output price, $/M tokens")
    requests: Mapped[Optional[float]] = mapped_column(Float, comment="Requests during the day (running total, may be prorated)")
    input_tokens: Mapped[Optional[float]] = mapped_column(Float, comment="Input tokens during the day (running total, may be prorated)")
    output_tokens: Mapped[Optional[float]] = mapped_column(Float, comment="Output tokens during the day (running total, may be prorated)")
    covered_until: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="Requests are counted up to this time (naive UTC)")
    input_tokens_covered_until: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="Input tokens are counted up to this time (naive UTC)")
    output_tokens_covered_until: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="Output tokens are counted up to this time (naive UTC)")
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="Part of the totals was backfilled from a longer window")
    final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="The day has closed and the row no longer changes")
    configured: Mapped[Optional[int]] = mapped_column(Integer, comment="Configured instances at the last probe")
    healthy: Mapped[Optional[int]] = mapped_column(Integer, comment="Healthy instances at the last probe")
    status: Mapped[Optional[str]] = mapped_column(String(16), comment="Model status at the last probe (ok, degraded, down)")
    polls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="Probes that listed the model during the day")
    polls_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="Probes where the model status was ok")
    healthy_sum: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="Sum of healthy instances over those probes")

    @property
    def tokens(self) -> float | None:
        """Input + output tokens; None until both are counted."""
        return token_sum(self.input_tokens, self.output_tokens)

    @property
    def tokens_covered_until(self) -> datetime | None:
        """Tokens are counted up to the earlier of the input and output watermarks."""
        if self.input_tokens_covered_until is None or self.output_tokens_covered_until is None:
            return None
        return min(self.input_tokens_covered_until, self.output_tokens_covered_until)

    # source = hardware: cost of running the model and the inputs used for it
    daily_cost: Mapped[Optional[float]] = mapped_column(Float, comment="Hardware + energy cost for the day, $")
    gpu: Mapped[Optional[str]] = mapped_column(String(64), comment="GPU type from the models: config")
    gpus: Mapped[Optional[int]] = mapped_column(Integer, comment="GPUs per instance")
    gpu_price: Mapped[Optional[float]] = mapped_column(Float, comment="$ per GPU per month")
    energy: Mapped[Optional[float]] = mapped_column(Float, comment="Watts per GPU")
    kwh: Mapped[Optional[float]] = mapped_column(Float, comment="$ per kWh")
    avg_healthy: Mapped[Optional[float]] = mapped_column(Float, comment="Average healthy instances (healthy_sum / polls)")


class Probe(Base):
    __tablename__ = "probes"
    __table_args__ = {"comment": "One row per GET /v1/models probe; pruned after the retention period"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    polled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True, comment="Probe start (naive UTC)")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="The probe got a valid response")
    response_time_ms: Mapped[Optional[float]] = mapped_column(Float, comment="Response time in milliseconds")
    model_count: Mapped[Optional[int]] = mapped_column(Integer, comment="Canonical models in the response")
    models_up: Mapped[Optional[int]] = mapped_column(Integer, comment="Canonical models whose status is not down")


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (
        Index("ix_samples_model_sampled_at", "model", "sampled_at"),
        {"comment": "One row per canonical model per successful probe; pruned after the retention period"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False, comment="Lumen model id")
    sampled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="Probe start (naive UTC)")
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="ok, degraded or down")
    configured: Mapped[Optional[int]] = mapped_column(Integer, comment="Configured instances")
    healthy: Mapped[Optional[int]] = mapped_column(Integer, comment="Healthy instances")
    requests_last_minute: Mapped[Optional[int]] = mapped_column(Integer, comment="Requests in the minute before the probe")
    input_tokens_last_minute: Mapped[Optional[int]] = mapped_column(Integer, comment="Input tokens in the minute before the probe, when Lumen reports them")
    output_tokens_last_minute: Mapped[Optional[int]] = mapped_column(Integer, comment="Output tokens in the minute before the probe, when Lumen reports them")

    @property
    def tokens_last_minute(self) -> int | None:
        """Input + output tokens in the minute before the probe; None until both are reported."""
        return token_sum(self.input_tokens_last_minute, self.output_tokens_last_minute)
