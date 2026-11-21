"""Tests for scripts/claude.sh with a fake `claude` that prints its environment."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "claude.sh"

FAKE_CLAUDE = """#!/bin/sh
for arg in "$@"; do printf 'ARG=%s\\n' "$arg"; done
env
"""

SWITCHES = {
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
    "MAX_THINKING_TOKENS": "0",
    "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
}


def _write_fake(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CLAUDE)
    path.chmod(0o755)


@pytest.fixture()
def home(tmp_path):
    """A temp HOME whose ~/.local/bin/claude is the fake, so the script's first
    choice is always the fake whatever is installed on the machine."""
    home = tmp_path / "home"
    _write_fake(home / ".local" / "bin" / "claude")
    return home


def run_script(home, env_extra=None, args=(), path_dir=None):
    env = {
        "HOME": str(home),
        "PATH": f"{path_dir}:/usr/bin:/bin" if path_dir else "/usr/bin:/bin",
        "LUMEN_BASE_URL": "http://127.0.0.1:5001",
        "LUMEN_API_KEY": "sk_test1234567890abcd",
        "ANTHROPIC_MODEL": "some-model",
    }
    env.update(env_extra or {})
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def parse(stdout):
    args, env = [], {}
    for line in stdout.splitlines():
        if line.startswith("ARG="):
            args.append(line[len("ARG="):])
        elif "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    return args, env


@pytest.mark.parametrize("missing", ["ANTHROPIC_MODEL", "LUMEN_API_KEY", "LUMEN_BASE_URL"])
def test_missing_setting_fails_and_names_it(home, missing):
    r = run_script(home, {missing: None})
    assert r.returncode != 0
    assert missing in r.stderr
    assert "ARG=" not in r.stdout and "ANTHROPIC_BASE_URL" not in r.stdout


def test_empty_model_fails(home):
    r = run_script(home, {"ANTHROPIC_MODEL": ""})
    assert r.returncode != 0
    assert "ANTHROPIC_MODEL" in r.stderr


def test_exports_lumen_settings_and_passes_arguments(home):
    r = run_script(home, args=["-p", "hello world", "--max-turns", "3"])
    assert r.returncode == 0, r.stderr
    args, env = parse(r.stdout)
    assert args == ["-p", "hello world", "--max-turns", "3"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:5001"
    assert env["ANTHROPIC_API_KEY"] == "sk_test1234567890abcd"
    assert env["ANTHROPIC_MODEL"] == "some-model"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "some-model"
    for key, value in SWITCHES.items():
        assert env[key] == value


def test_haiku_model_override(home):
    r = run_script(home, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "small-model"})
    assert r.returncode == 0, r.stderr
    _, env = parse(r.stdout)
    assert env["ANTHROPIC_MODEL"] == "some-model"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "small-model"


@pytest.mark.skipif(os.path.exists("/usr/local/bin/claude"),
                    reason="/usr/local/bin/claude would be chosen before PATH")
def test_falls_back_to_claude_on_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    _write_fake(bin_dir / "claude")
    r = run_script(home, args=["--version"], path_dir=bin_dir)
    assert r.returncode == 0, r.stderr
    args, env = parse(r.stdout)
    assert args == ["--version"]
    assert env["ANTHROPIC_MODEL"] == "some-model"


def _write_ini(home, text, config_dir=None):
    ini = (config_dir or home / ".config" / "lumen") / "config.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_text(text)
    return ini


def test_settings_come_from_config_ini(home):
    _write_ini(home, "# note\n[other]\nchat_model = wrong\n\n[lumen]\n"
                     "base_url = http://ini.example:5001\napi_key: sk_ini_key_00000\n"
                     "chat_model=ini-model\n")
    r = run_script(home, {"LUMEN_BASE_URL": None, "LUMEN_API_KEY": None,
                          "ANTHROPIC_MODEL": None})
    assert r.returncode == 0, r.stderr
    _, env = parse(r.stdout)
    assert env["ANTHROPIC_BASE_URL"] == "http://ini.example:5001"
    assert env["ANTHROPIC_API_KEY"] == "sk_ini_key_00000"
    assert env["ANTHROPIC_MODEL"] == "ini-model"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "ini-model"


def test_environment_wins_over_config_ini(home):
    _write_ini(home, "[lumen]\nbase_url = http://ini.example\napi_key = sk_ini\n"
                     "chat_model = ini-model\n")
    r = run_script(home)
    assert r.returncode == 0, r.stderr
    _, env = parse(r.stdout)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:5001"
    assert env["ANTHROPIC_API_KEY"] == "sk_test1234567890abcd"
    assert env["ANTHROPIC_MODEL"] == "some-model"


def test_config_dir_override(home, tmp_path):
    other = tmp_path / "elsewhere"
    _write_ini(home, "[lumen]\nchat_model = home-model\n")
    _write_ini(home, "[lumen]\nchat_model = other-model\n", config_dir=other)
    r = run_script(home, {"ANTHROPIC_MODEL": None, "LUMEN_CONFIG_DIR": str(other)})
    assert r.returncode == 0, r.stderr
    assert parse(r.stdout)[1]["ANTHROPIC_MODEL"] == "other-model"


def test_missing_model_error_names_config_ini(home):
    r = run_script(home, {"ANTHROPIC_MODEL": None})
    assert r.returncode != 0
    assert "chat_model" in r.stderr and "config.ini" in r.stderr
