"""Locust load test for the Lumen API.

Configuration is read from config.yaml in the same directory.
Each simulated user is assigned a different API key round-robin,
so N keys give you N × (rate limit) effective throughput.

Usage:
  # Web UI (interactive)
  uv run locust

  # Headless
  uv run locust --headless -u 10 -r 2 --run-time 60s
"""
import itertools
import os
import time
from pathlib import Path

import yaml
from locust import HttpUser, between, task

_config_path = Path(__file__).parent / "config.yaml"
with _config_path.open() as f:
    _config = yaml.safe_load(f)

_keys = _config["api_keys"]
_model = _config["model"]
_prompt = _config["prompt"]
_base_url = _config["base_url"]

# Cycle through keys; each user picks the next available one.
_key_cycle = itertools.cycle(_keys)
_key_lock = __import__("threading").Lock()


def _next_key() -> str:
    with _key_lock:
        return next(_key_cycle)


class LumenUser(HttpUser):
    host = _base_url
    wait_time = between(1, 3)

    def on_start(self):
        self._api_key = _next_key()
        self.client.headers.update({"Authorization": f"Bearer {self._api_key}"})

    @task
    def chat_completion(self):
        payload = {
            "model": _model,
            "messages": [{"role": "user", "content": _prompt}],
            "stream": False,
        }
        start = time.monotonic()
        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            catch_response=True,
        ) as resp:
            elapsed = time.monotonic() - start
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage", {})
                total = usage.get("total_tokens", "?")
                resp.success()
            elif resp.status_code == 429:
                resp.failure(f"Rate limited (429): {resp.text[:120]}")
            else:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:120]}")
