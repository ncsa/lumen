"""Shared fixtures for the browser-driven UI tests.

`browser` is Playwright's Chromium (`uv run python -m playwright install chromium`)
or, failing that, the system Google Chrome. Outside CI the tests that use it
skip when neither starts; in CI (the CI env var is set) they fail instead.
"""
import os

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        errors = []
        # Playwright's own Chromium if installed, else the system Google Chrome
        # (preinstalled on GitHub's Ubuntu runners).
        for channel in (None, "chrome"):
            try:
                b = pw.chromium.launch(channel=channel)
                break
            except PlaywrightError as e:
                errors.append(str(e).splitlines()[0])
        else:
            if os.environ.get("CI"):
                pytest.fail(f"No browser could start: {errors}")
            pytest.skip(f"No browser could start: {errors}")
        yield b
        b.close()
