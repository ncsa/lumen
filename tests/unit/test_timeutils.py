"""as_utc_aware / as_utc_naive accept both naive (UTC by convention) and aware input."""
from datetime import datetime, timedelta, timezone

from lumen.timeutils import as_utc_aware, as_utc_naive

NAIVE = datetime(2026, 10, 1, 12, 0, 0, 123456)
AWARE = NAIVE.replace(tzinfo=timezone.utc)
CHICAGO = AWARE.astimezone(timezone(timedelta(hours=-5)))


def test_as_utc_aware():
    assert as_utc_aware(NAIVE) == AWARE
    assert as_utc_aware(NAIVE).tzinfo is timezone.utc
    assert as_utc_aware(AWARE) == AWARE
    converted = as_utc_aware(CHICAGO)
    assert converted.tzinfo is timezone.utc and converted.hour == 12


def test_as_utc_naive():
    assert as_utc_naive(NAIVE) is NAIVE
    assert as_utc_naive(AWARE) == NAIVE
    assert as_utc_naive(AWARE).tzinfo is None
    assert as_utc_naive(CHICAGO) == NAIVE
