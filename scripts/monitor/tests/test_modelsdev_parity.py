"""The monitor's models.dev matcher must pick what Lumen's picks.

`monitor/modelsdev.py` is a copy of the matcher in `lumen/services/model_sync.py`
(see AGENTS.md). This loads Lumen's module from the repository by path, without
importing the `lumen` package, and runs both on the same fixture. It is skipped
when the monitor is tested outside the repository (e.g. from the wheel).
"""

import importlib.util
from pathlib import Path

import pytest

from monitor import modelsdev

MODEL_SYNC = Path(__file__).resolve().parents[3] / "lumen" / "services" / "model_sync.py"

FIXTURE = [
    {"id": "deepseek-coder-v1", "name": "DeepSeek Coder V1"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
    {"id": "qwen3-coder-30b-a3b-instruct", "name": "Qwen3 Coder 30B A3B"},
    {"id": "qwen2.5-coder-32b-instruct", "name": "Qwen2.5 Coder 32B"},
    {"id": "gemma-3-27b-it", "name": "Gemma 3 27B"},
    {"id": "granite-4.0-tiny", "name": "Granite 4.0 Tiny"},
    {"id": "qwen2-audio-instruct", "name": "Qwen2 Audio"},
    {"id": "glm-4.6", "name": "GLM 4.6"},
    {"id": "gpt-oss-120b", "name": "GPT OSS 120B"},
    {"id": "mixtral-8x7b-instruct", "name": "Mixtral 8x7B"},
]

# (Lumen id, upstream root, the listing both matchers must pick)
CASES = [
    ("deepseek-coder", "deepseek-ai/deepseek-coder-v1-instruct", "deepseek-coder-v1"),
    ("deepseek-coder-v2", "deepseek-ai/DeepSeek-Coder-V2-Instruct", None),  # v2 never matches v1
    ("deepseek-v4.1-flash", "deepseek-ai/DeepSeek-V4.1-Flash-FP8", "deepseek-v4-flash"),
    ("deepseek-v5-flash", "deepseek-ai/DeepSeek-V5-Flash", None),  # v5 never matches v4
    ("qwen3-coder", "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8", "qwen3-coder-30b-a3b-instruct"),
    ("qwen4-coder", "Qwen/Qwen4-Coder-30B-A3B-Instruct", None),  # qwen4 never matches qwen3
    ("gemma-3-27b-it", "google/gemma-3-27b-it", "gemma-3-27b-it"),
    ("granite-speech", "ibm-granite/granite-speech-4.1-2b-plus", None),  # kind guard: speech vs text
    ("qwen2-audio", "Qwen/Qwen2-Audio-7B-Instruct", "qwen2-audio-instruct"),  # same kind still matches
    ("qwen2-vision", "Qwen/Qwen2-Vision-7B", None),  # kind guard: vision vs audio/text
    ("glm-4.6", "zai-org/GLM-4.6-FP8", "glm-4.6"),  # exact normalized id
    ("gpt-oss-120b", "openai/gpt-oss-120b", "gpt-oss-120b"),
    ("mixtral", "mistralai/Mixtral-8x7B-Instruct-v0.1", "mixtral-8x7b-instruct"),  # 8x7b is a size, not a version
]


@pytest.fixture(scope="module")
def model_sync():
    if not MODEL_SYNC.exists():
        pytest.skip("lumen/services/model_sync.py is not next to the monitor")
    spec = importlib.util.spec_from_file_location("lumen_model_sync", MODEL_SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("model_id", "root", "expected"), CASES)
def test_same_pick_as_lumen(model_sync, model_id, root, expected):
    lumen_pick = model_sync.find_in_modelsdev(root, FIXTURE, config_name=model_id)
    monitor_pick = modelsdev.match(model_id, root, FIXTURE)
    assert monitor_pick == lumen_pick
    assert (monitor_pick or {}).get("id") == expected


@pytest.mark.parametrize("value", [
    "deepseek-coder-v2", "DeepSeek-V4.1-Flash-FP8", "Qwen3-Coder-30B-A3B-Instruct-FP8", "mixtral-8x7b-v0.1",
    "gpt-4o-2024-08-06", "model-q5_k_m", "llama-3.3-70b-int4", "Ornith-1.0-35B-FP8", "qwen2.5:latest",
])
def test_same_versions_as_lumen(model_sync, value):
    assert modelsdev._versions(value) == model_sync._versions(value)


def test_versions_ignore_sizes_and_precisions():
    assert modelsdev._versions("deepseek-coder-v2") == {"2"}
    assert modelsdev._versions("DeepSeek-V4.1-Flash-FP8") == {"4"}
    assert modelsdev._versions("Qwen3-Coder-30B-A3B-Instruct-FP8") == {"3"}
    assert modelsdev._versions("mixtral-8x7b-bf16-int4-q5_k_m") == set()
