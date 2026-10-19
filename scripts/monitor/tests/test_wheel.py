"""The built wheel must migrate on its own, outside the source checkout.

An editable install reads alembic.ini and migrations/ from the checkout, so
only an installed wheel shows whether those resources are packaged.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(UV is None, reason="uv is not installed")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([UV, "build", "--wheel", "--out-dir", str(out), str(PROJECT)], check=True, capture_output=True)
    return next(out.glob("*.whl"))


def test_wheel_contains_migrations(wheel):
    names = set(zipfile.ZipFile(wheel).namelist())
    assert "monitor/alembic.ini" in names
    assert "monitor/migrations/env.py" in names
    assert "monitor/migrations/script.py.mako" in names
    assert any(n.startswith("monitor/migrations/versions/") and n.endswith(".py") for n in names)


def test_wheel_contains_the_dashboard(wheel):
    names = set(zipfile.ZipFile(wheel).namelist())
    assert {"monitor/templates/overview.html", "monitor/static/overview.js", "monitor/static/monitor.css"} <= names


@pytest.mark.timeout(300)
def test_installed_wheel_migrates_outside_checkout(wheel, tmp_path):
    venv = tmp_path / "venv"
    subprocess.run([UV, "venv", "--python", sys.executable, str(venv)], check=True, capture_output=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([UV, "pip", "install", "--python", str(python), str(wheel)], check=True, capture_output=True)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    db = run_dir / "monitor.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MONITOR_", "PYTHON", "VIRTUAL_ENV"))}
    env["MONITOR_DATABASE_URL"] = f"sqlite:///{db}"
    result = subprocess.run([str(python), "-I", "-m", "monitor.cli", "migrate"], cwd=run_dir, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    # The package was imported from the venv, not from the checkout.
    where = subprocess.run([str(python), "-I", "-c", "import monitor; print(monitor.__file__)"], cwd=run_dir, env=env, capture_output=True, text=True, check=True)
    assert str(PROJECT) not in where.stdout

    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"daily_stats", "probes", "samples", "alembic_version"} <= tables
