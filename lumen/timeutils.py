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
