"""Runs the dashboard's JS tests (`tests/js/*.test.mjs`) with Node's built-in test runner."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
JS_TESTS = Path(__file__).resolve().parent / "js"


def test_overview_js():
    if NODE is None:
        # CI runners ship node, so a missing one there is a setup error, not a reason to skip.
        assert not os.environ.get("CI"), "node is required in CI"
        pytest.skip("node is not installed")
    result = subprocess.run([NODE, "--test", *sorted(map(str, JS_TESTS.glob("*.test.mjs")))], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
