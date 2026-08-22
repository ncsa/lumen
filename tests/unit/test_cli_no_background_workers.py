"""Background workers must not start when a CLI command loads the app.

Every `flask` command runs create_app() to get the config and db binding, but
those processes never serve requests. For `flask db upgrade` it is actively
harmful: the workers query the database with the current models while the
schema is still on the previous revision, so a migration that adds a column
races a burst of "column ... does not exist" tracebacks.

`flask run` is the one command that does serve, so it keeps its workers.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import click
import pytest

from lumen import _loaded_by_non_serving_cli

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_simulated_contexts_carry_an_info_name():
    """click.Context defaults info_name to None, which would make the
    simulated-context tests below vacuous."""
    with click.Context(click.Command("upgrade"), info_name="upgrade") as ctx:
        assert ctx.info_name == "upgrade"


def test_no_click_context_is_a_serving_process():
    """`uv run lumen` and the tests import the app with no click context."""
    assert _loaded_by_non_serving_cli() is False


def test_uvicorn_launch_is_a_serving_process(monkeypatch):
    """uvicorn's console script is itself a click command, so asgi.py's
    create_app() runs inside a live click context named "uvicorn". Only
    Flask's CLI sets FLASK_RUN_FROM_CLI, so without it the context must be
    ignored — otherwise every production replica silently loses its
    background workers (confirmed empirically under `uvicorn asgi:app`)."""
    monkeypatch.delenv("FLASK_RUN_FROM_CLI", raising=False)
    with click.Context(click.Command("uvicorn"), info_name="uvicorn"):
        assert _loaded_by_non_serving_cli() is False


@pytest.mark.parametrize("command", ["upgrade", "db", "init-db", "reassign-model", "routes", "shell"])
def test_cli_commands_are_non_serving(command, monkeypatch):
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")
    with click.Context(click.Command(command), info_name=command):
        assert _loaded_by_non_serving_cli() is True


def test_flask_run_keeps_its_workers(monkeypatch):
    """`flask run` serves requests, so it must not be caught by the guard."""
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")
    with click.Context(click.Command("run"), info_name="run"):
        assert _loaded_by_non_serving_cli() is False


def test_real_cli_invocation_starts_no_threads(tmp_path):
    """End to end through the actual flask CLI, not a simulated context.

    Guards the detection mechanism itself: the app module in this repo is named
    "run" ("flask --app run db upgrade"), so an argv-based check would see the
    word "run" and wrongly treat a migration as a serving process.
    """
    probe = tmp_path / "probe_app.py"
    probe.write_text(textwrap.dedent("""
        import threading, time, click
        from lumen import create_app

        @click.command("probe")
        def probe():
            time.sleep(0.4)
            live = [t for t in threading.enumerate() if t is not threading.main_thread()]
            click.echo(f"THREADS={len(live)}")

        app = create_app()
        app.cli.add_command(probe)
    """))
    env = {
        "PATH": __import__("os").environ["PATH"],
        "HOME": __import__("os").environ.get("HOME", ""),
        "CONFIG_YAML": str(REPO_ROOT / "tests" / "fixtures" / "test_config.yaml"),
        "PYTHONPATH": f"{tmp_path}{__import__('os').pathsep}{REPO_ROOT}",
        # deliberately NOT setting BACKGROUND_WORKER: the guard must stand alone
    }
    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "probe_app", "probe"],
        capture_output=True, text=True, timeout=120, cwd=tmp_path, env=env,
    )
    assert "THREADS=0" in result.stdout, (
        f"a flask CLI command started background threads.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr[-2000:]}"
    )
