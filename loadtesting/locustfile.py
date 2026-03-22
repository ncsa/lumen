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
import random
import time
from pathlib import Path

import yaml
from locust import HttpUser, between, task

_config_path = Path(__file__).parent / "config.yaml"
with _config_path.open() as f:
    _config = yaml.safe_load(f)

_keys = _config["api_keys"]
_model = _config["model"]
_base_url = _config["base_url"]
_questions = _config.get("questions", ["static"])
_static_prompts = _config.get("prompts", ["Say hello in one sentence."])

# Cycle through keys; each user picks the next available one.
_key_cycle = itertools.cycle(_keys)
_key_lock = __import__("threading").Lock()

_OPS = ["+", "-", "*", "/"]


def _math_question() -> str:
    """Generate a random arithmetic question with 1–3 operation groups."""
    num_groups = random.randint(1, 3)
    groups = []
    for _ in range(num_groups):
        a = random.randint(1, 50)
        b = random.randint(1, 50)
        op = random.choice(_OPS)
        # Avoid division by zero
        if op == "/" and b == 0:
            b = 1
        groups.append(f"({a} {op} {b})")

    if len(groups) == 1:
        expr = groups[0].strip("()")
    else:
        connectors = [random.choice(["+", "-", "*"]) for _ in range(len(groups) - 1)]
        expr = groups[0]
        for connector, group in zip(connectors, groups[1:]):
            expr = f"{expr} {connector} {group}"

    return f"What is {expr}? Just give the numeric answer."


def _next_key() -> str:
    with _key_lock:
        return next(_key_cycle)


def _get_prompt() -> str:
    question_type = random.choice(_questions)
    if question_type == "math":
        return _math_question()
    return random.choice(_static_prompts)


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
            "messages": [{"role": "user", "content": _get_prompt()}],
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
