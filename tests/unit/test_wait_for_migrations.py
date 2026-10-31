"""``flask wait-for-migrations``: the gate the serve step runs before it starts.

SQLite databases stamped by hand stand in for a migrated one; the command only reads
``alembic_version``, so no real migration is needed. The clock is faked: ``sleep``
advances it instead of waiting, so a test can let hours pass in a millisecond.
"""
import socket
import time

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.exc import OperationalError

from lumen import commands
from lumen.commands import wait_for_migrations_cmd
from tests.unit.test_migrations import _script_dir

HEAD = _script_dir().get_current_head()
# A merge revision has a tuple of parents; any one of them is behind the head.
_parent = _script_dir().get_revision(HEAD).down_revision
BEFORE_HEAD = _parent[0] if isinstance(_parent, tuple) else _parent


@pytest.fixture
def stamp(app):
    """Stamp the test database at a revision; ``stamp(None)`` leaves it unstamped."""
    from lumen.extensions import db

    def _stamp(rev):
        with app.app_context(), db.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
            if rev is not None:
                conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
                conn.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": rev})

    yield _stamp
    _stamp(None)


class FakeClock:
    """``time.monotonic`` and ``time.sleep`` for the command; ``sleep`` only moves the clock."""

    def __init__(self, step=None, on_sleep=None):
        self.now = 1000.0
        self.sleeps = 0
        self.step = step
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += self.step if self.step is not None else seconds
        if self.on_sleep:
            self.on_sleep(self)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(commands.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(commands.time, "sleep", fake.sleep)
    return fake


def _run(app, *args):
    return app.test_cli_runner().invoke(wait_for_migrations_cmd, list(args))


def test_registered_on_the_app(app):
    assert app.cli.commands["wait-for-migrations"] is wait_for_migrations_cmd


def test_at_head_exits_zero_without_waiting(app, stamp, clock):
    stamp(HEAD)
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert f"at migration head {HEAD}" in result.output
    assert clock.sleeps == 0


def test_behind_times_out(app, stamp, clock):
    stamp(BEFORE_HEAD)
    result = _run(app, "--timeout", "5")
    assert result.exit_code == 1
    assert f"database is at revision {BEFORE_HEAD}, expected {HEAD}" in result.output
    assert "timed out after 5s" in result.output
    assert "lumen-upgrade" in result.output
    # Retries every 2 seconds: at t=0, 2, 4, then 6 is past the timeout.
    assert clock.sleeps == 3


def test_unstamped_database_times_out(app, stamp, clock):
    stamp(None)
    result = _run(app, "--timeout", "1")
    assert result.exit_code == 1
    assert f"database is at revision none, expected {HEAD}" in result.output


def test_unknown_revision_fails_immediately(app, stamp, clock):
    stamp("f00dfeedbeef")
    result = _run(app)  # no --timeout: would wait forever if it treated this as "behind"
    assert result.exit_code == 1
    assert "f00dfeedbeef, which this image does not know" in result.output
    assert clock.sleeps == 0


def test_database_catches_up_while_waiting(app, stamp, monkeypatch):
    stamp(BEFORE_HEAD)

    def upgrade_on_third_sleep(fake):
        if fake.sleeps == 3:
            stamp(HEAD)

    fake = FakeClock(on_sleep=upgrade_on_third_sleep)
    monkeypatch.setattr(commands.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(commands.time, "sleep", fake.sleep)
    result = _run(app, "--timeout", "60")
    assert result.exit_code == 0, result.output
    assert "Waiting for migrations" in result.output
    assert f"at migration head {HEAD}" in result.output
    assert fake.sleeps == 3


def test_without_timeout_waits_indefinitely(app, stamp, monkeypatch):
    """A week of fake time passes without a timeout; then the upgrade lands and it exits 0."""
    stamp(BEFORE_HEAD)
    week = 7 * 24 * 3600

    def upgrade_after_a_week(fake):
        if fake.now - 1000.0 >= week:
            stamp(HEAD)

    fake = FakeClock(step=3600, on_sleep=upgrade_after_a_week)
    monkeypatch.setattr(commands.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(commands.time, "sleep", fake.sleep)
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert fake.sleeps == week // 3600
    # The state is logged once, not every retry; the upgrade hint repeats.
    assert result.output.count("Waiting for migrations:") == 1
    assert result.output.count("Still waiting:") == fake.sleeps - 1


def test_hint_about_every_30_seconds(app, stamp, clock):
    stamp(BEFORE_HEAD)
    result = _run(app, "--timeout", "95")
    assert result.exit_code == 1
    # Checks at t=0, 2, ..., 94; hints once 30 s have passed since the last: t=30, 60, 90.
    assert result.output.count("Still waiting:") == 3


def test_is_read_only(app, stamp, clock):
    stamp(BEFORE_HEAD)
    _run(app, "--timeout", "1")
    from lumen.extensions import db
    with app.app_context(), db.engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == BEFORE_HEAD


def test_unreachable_database_keeps_waiting(app, monkeypatch, clock):
    """An unreachable database is not an error yet: the database may still be starting."""
    class Down:
        url = make_url("sqlite://")

        def connect(self):
            raise OperationalError("connect", {}, ConnectionRefusedError("connection refused"))

    migrate = app.extensions["migrate"]
    monkeypatch.setattr(migrate, "db", type("DB", (), {"engine": Down()})())
    result = _run(app, "--timeout", "3")
    assert result.exit_code == 1
    assert "database unreachable (ConnectionRefusedError: connection refused)" in result.output
    assert clock.sleeps == 2


def test_nan_timeout_is_rejected(app, stamp, clock):
    """NaN compares False with everything, so it would turn --timeout into "wait forever"."""
    stamp(BEFORE_HEAD)
    result = _run(app, "--timeout", "nan")
    assert result.exit_code == 2
    assert "not NaN" in result.output
    assert clock.sleeps == 0


def test_silent_postgres_host_does_not_outlast_the_timeout(app, monkeypatch):
    """A host that accepts TCP and never answers must not block one check forever.

    No PostgreSQL needed: a listening socket that never accepts leaves libpq waiting
    for the server's first message, which only ``connect_timeout`` ends. Real clock:
    the fake one patches the shared ``time`` module, which psycopg's own wait reads.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    class Silent:
        url = make_url(f"postgresql://lumen:lumen@127.0.0.1:{port}/lumen")

    migrate = app.extensions["migrate"]
    monkeypatch.setattr(migrate, "db", type("DB", (), {"engine": Silent()})())
    monkeypatch.setattr(commands, "WAIT_CONNECT_TIMEOUT_SECONDS", 2)  # libpq's minimum
    started = time.time()
    try:
        result = _run(app, "--timeout", "1")
    finally:
        listener.close()
    assert result.exit_code == 1, result.output
    assert "database unreachable" in result.output
    assert time.time() - started < 15
