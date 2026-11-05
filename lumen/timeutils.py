from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (no tzinfo).

    The whole app stores time in UTC. DB timestamp columns are kept naive so
    values behave identically on SQLite and PostgreSQL; conversion to the
    user's local timezone happens only at display time (see static/app.js).
    Use this everywhere a UTC "now" is needed instead of repeating
    ``datetime.now(timezone.utc).replace(tzinfo=None)``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc_aware(dt: datetime) -> datetime:
    """Return ``dt`` as an aware UTC datetime.

    Naive input is taken to be UTC already (the app's storage convention);
    aware input is converted to UTC. Needed where naive app timestamps meet
    ``request_logs.time``, which is aware on PostgreSQL and comes back naive
    from SQLite.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_utc_naive(dt: datetime) -> datetime:
    """Return ``dt`` as a naive UTC datetime, the form every app table stores.

    Naive input is returned unchanged (it is UTC by convention); aware input
    is converted to UTC and stripped of its tzinfo.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
