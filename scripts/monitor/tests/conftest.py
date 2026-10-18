import os

import pytest


@pytest.fixture
def postgres_url():
    """A throwaway PostgreSQL database; tests using it are marked `postgres`."""
    url = os.environ.get("MONITOR_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("MONITOR_TEST_POSTGRES_URL is not set")
    return url
