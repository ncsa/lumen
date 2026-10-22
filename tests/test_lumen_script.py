"""End-to-end tests for lumen.sh against a fake Lumen server."""
import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

SCRIPT = "lumen.sh"
TEST_KEY = "sk_test1234567890abcd"

MODELS = {
    "object": "list",
    "data": [
        {
            "id": "gpt-demo", "max_model_len": 4000, "max_output_tokens": 500,
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "supports_function_calling": True,
            "supports_reasoning": True,
        },
        {
            "id": "gpt-alias", "max_model_len": 4000,
            "input_cost_per_million": 1.0, "output_cost_per_million": 2.0,
            "parent": "gpt-demo",
            "output_modalities": ["text"],
        },
    ],
}


class FakeLumen(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        s = self.server
        s.hits[self.path] = s.hits.get(self.path, 0) + 1
        if self.path == "/oauth/device_authorization":
            assert form.get("client_id") and form.get("name")
            self._json(200, {
                "device_code": "devcode-1", "user_code": "QQQQ-QQQQ",
                "verification_uri": "http://127.0.0.1:1/device",
                "interval": 1, "expires_in": 30,
                **s.auth_extra,
            })
        elif self.path == "/oauth/token":
            assert form["grant_type"][0] == "urn:ietf:params:oauth:grant-type:device_code"
            code, obj, *headers = s.token_queue.pop(0) if s.token_queue else (400, {"error": "expired_token"})
            self._json(code, obj, *headers)
        else:
            self._json(404, {"error": "not_found"})

    def do_GET(self):
        if self.path == "/v1/models":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer sk_"):
                self._json(401, {"error": {"message": "no key"}})
                return
            self.server.hits["/v1/models"] = self.server.hits.get("/v1/models", 0) + 1
            self._json(200, MODELS)
        else:
            self._json(404, {"error": "not_found"})


@pytest.fixture()
def lumen_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLumen)
    server.hits = {}
    server.token_queue = []
    server.auth_extra = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text("{}\n")
    return home


def run_script(home, server_port_or_url=None, args=(), env_extra=None, input=b"", timeout=30):
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        "SHELL": "/bin/bash",
    }
    if home is not None:
        env["HOME"] = str(home)
        env["OPENCODE_CONFIG"] = str(home / ".config" / "opencode" / "opencode.json")
    if server_port_or_url:
        url = server_port_or_url if "://" in str(server_port_or_url) else f"http://127.0.0.1:{server_port_or_url}"
        env["LUMEN_BASE_URL"] = url
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", SCRIPT, *args], env=env, input=input,
        capture_output=True, timeout=timeout,
    )


def host_key(port):
    return f"127.0.0.1:{port}"


# --- server URL validation -------------------------------------------------

def test_http_rejected_for_non_local_server():
    r = run_script(None, "http://lumen.example.edu", args=["--no-opencode"])
    assert r.returncode != 0
    assert b"https" in r.stderr


def test_traversal_path_rejected(lumen_server):
    port = lumen_server.server_address[1]
    r = run_script(None, f"http://127.0.0.1:{port}/../evil", args=["--no-opencode"])
    assert r.returncode != 0
    assert b"'..'" in r.stderr


def test_empty_host_rejected_before_whitelist():
    r = run_script(None, "https:///x", args=["--no-opencode"])
    assert r.returncode != 0
    assert b"invalid server URL" in r.stderr


def test_empty_path_segment_rejected():
    r = run_script(None, "https://x.example.edu//api", args=["--no-opencode"])
    assert r.returncode != 0
    assert b"empty segments" in r.stderr


@pytest.mark.parametrize("url", [
    "https://x.example.edu/a b",
    "https://x.example.edu/$(id)",
    "https://x.example.edu/api?x=1",
    "https://user@x.example.edu",
    "https://x.example.edu/\x1b[31m",
])
def test_server_url_character_whitelist(url):
    r = run_script(None, url, args=["--no-opencode"])
    assert r.returncode != 0
    assert b"invalid characters" in r.stderr
    assert b"\x1b" not in r.stderr


def test_server_url_whitelist_allows_bracketed_ipv6(home):
    r = run_script(home, "http://[::1]:9/api", args=["--no-opencode"],
                   env_extra={"LUMEN_API_KEY": "sk_ipv6_key_000000"})
    assert r.returncode == 0, r.stderr
    assert b"LUMEN_API_KEY=sk_ipv6_key_000000" in r.stdout


def test_bad_server_url_echoed_without_control_characters():
    r = run_script(None, "http://evil\x1b[31mx", args=["--no-opencode"])
    assert r.returncode != 0
    assert b"\x1b" not in r.stderr


# --- device flow ------------------------------------------------------------

def test_device_flow_stores_key_and_rc_block(home, lumen_server, tmp_path):
    port = lumen_server.server_address[1]
    env, sleeps = fake_bin(tmp_path)
    lumen_server.token_queue = [
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": TEST_KEY, "token_type": "Bearer",
               "approved_by": "alice@example.edu"}),
    ]
    r = run_script(home, port, args=["--no-opencode"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert logged_sleeps(sleeps) == [1, 0, 1]
    assert b"QQQQ-QQQQ" in r.stdout
    assert b"alice@example.edu" in r.stdout
    keys = json.loads((home / ".config" / "lumen" / "keys.json").read_text())
    assert keys[host_key(port)] == TEST_KEY
    bashrc = (home / ".bashrc").read_text()
    assert "# >>> lumen >>>" in bashrc
    assert f"export LUMEN_API_KEY='{TEST_KEY}'" in bashrc
    assert f"export LUMEN_BASE_URL='http://127.0.0.1:{port}'" in bashrc
    import stat
    mode = stat.S_IMODE((home / ".config" / "lumen" / "keys.json").stat().st_mode)
    assert mode == 0o600
    assert f"LUMEN_API_KEY={TEST_KEY}" in r.stdout.decode()


def test_second_run_reuses_stored_key(home, lumen_server):
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_stored_key_000"}))
    r = run_script(home, port, args=["--no-opencode"])
    assert r.returncode == 0, r.stderr
    assert "sk_stored_key_000" in r.stdout.decode()
    assert "/oauth/device_authorization" not in lumen_server.hits


def test_env_key_wins_over_store(home, lumen_server):
    port = lumen_server.server_address[1]
    r = run_script(home, port, args=["--no-opencode"],
                   env_extra={"LUMEN_API_KEY": "sk_from_env_00000"})
    assert r.returncode == 0, r.stderr
    assert "sk_from_env_00000" in r.stdout.decode()
    assert not (home / ".config" / "lumen").exists()


def test_relogin_replaces_stored_key_without_prompt(home, lumen_server):
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_old_key_0000000"}))
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode", "--relogin"], input=b"")
    assert r.returncode == 0, r.stderr
    assert json.loads(keys.read_text())[host_key(port)] == TEST_KEY
    bashrc = (home / ".bashrc").read_text()
    assert bashrc.count("# >>> lumen >>>") == 1
    assert f"export LUMEN_API_KEY='{TEST_KEY}'" in bashrc
    assert "sk_old_key" not in bashrc


@pytest.mark.parametrize("shell_name,rc_file,line_fmt", [
    ("zsh", ".zshrc", "export LUMEN_{k}='{v}'"),
    ("csh", ".cshrc", "setenv LUMEN_{k} '{v}'"),
    ("fish", ".config/fish/config.fish", "set -gx LUMEN_{k} '{v}'"),
])
def test_rc_block_pairs_key_and_server_per_shell(home, lumen_server, shell_name, rc_file, line_fmt):
    port = lumen_server.server_address[1]
    (home / ".config" / "fish").mkdir(parents=True, exist_ok=True)
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"],
                   env_extra={"SHELL": f"/bin/{shell_name}"})
    assert r.returncode == 0, r.stderr
    rc = (home / rc_file).read_text()
    assert line_fmt.format(k="API_KEY", v=TEST_KEY) in rc
    assert line_fmt.format(k="BASE_URL", v=f"http://127.0.0.1:{port}") in rc


@pytest.mark.parametrize("existing_mode", [None, 0o644, 0o600])
def test_rc_file_restricted_to_owner(home, lumen_server, existing_mode):
    port = lumen_server.server_address[1]
    bashrc = home / ".bashrc"
    if existing_mode is not None:
        bashrc.write_text("alias ll='ls -l'\n")
        bashrc.chmod(existing_mode)
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    # A permissive umask, so a newly created rc file would default to 644.
    old_umask = os.umask(0o022)
    try:
        r = run_script(home, port, args=["--no-opencode"])
    finally:
        os.umask(old_umask)
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(bashrc.stat().st_mode) == 0o600
    assert f"export LUMEN_API_KEY='{TEST_KEY}'" in bashrc.read_text()
    assert (b"readable by other users" in r.stderr) == (existing_mode == 0o644)


@pytest.mark.parametrize("field", ["interval", "expires_in"])
def test_non_numeric_timing_values_are_not_evaluated(home, lumen_server, tmp_path, field):
    port = lumen_server.server_address[1]
    marker = tmp_path / "INJECTED"
    lumen_server.auth_extra = {field: f"BASH_VERSINFO[$(touch {marker})]"}
    lumen_server.token_queue = [
        (400, {"error": "slow_down"}),
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ] if field == "expires_in" else [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    env, sleeps = fake_bin(tmp_path)
    r = run_script(home, port, args=["--no-opencode"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    # interval falls back to the default of 5; slow_down raises 1 to 6.
    assert logged_sleeps(sleeps) == ([5] if field == "interval" else [1, 6, 6])


@pytest.mark.parametrize("token", ['sk_x"; touch INJECTED; "', "sk_$(id)", "not_sk_token"])
def test_malformed_access_token_rejected(home, lumen_server, token):
    port = lumen_server.server_address[1]
    lumen_server.token_queue = [
        (200, {"access_token": token, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"])
    assert r.returncode != 0
    assert b"unexpected token response" in r.stderr
    assert not (home / ".bashrc").exists()
    assert not (home / ".config" / "lumen" / "keys.json").exists()


def test_rc_block_values_are_literal_when_sourced(home, lumen_server):
    port = lumen_server.server_address[1]
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"])
    assert r.returncode == 0, r.stderr
    out = subprocess.run(
        ["bash", "-c", 'source "$1"; printf "%s\\n%s" "$LUMEN_API_KEY" "$LUMEN_BASE_URL"',
         "_", str(home / ".bashrc")],
        capture_output=True, check=True, env={"PATH": "/usr/bin:/bin"},
    ).stdout.decode()
    assert out == f"{TEST_KEY}\nhttp://127.0.0.1:{port}"


def rc_quote(value, shell):
    """Runs lumen.sh's rc_quote on its own, without the rest of the script."""
    prog = 'eval "$(sed -n "/^rc_quote()/,/^}/p" "$0")"; rc_quote "$1" "$2"'
    return subprocess.run(
        ["bash", "-c", prog, SCRIPT, value, shell],
        capture_output=True, check=True, env={"PATH": "/usr/bin:/bin"},
    ).stdout.decode()


@pytest.mark.parametrize("shell,value,expected", [
    ("bash", "https://h/a'b", "'https://h/a'\\''b'"),
    ("csh", "https://h/a'b", "'https://h/a'\\''b'"),
    ("bash", "https://h/a\\", "'https://h/a\\'"),
    ("fish", "https://h/a'b", "'https://h/a\\'b'"),
    ("fish", "https://h/a\\", "'https://h/a\\\\'"),
])
def test_rc_quote_emitted_bytes(shell, value, expected):
    assert rc_quote(value, shell) == expected


@pytest.mark.parametrize("value", ["a'b", "it's $(id) `id` \\ %s", "trailing\\", "''"])
def test_rc_quote_round_trips_in_bash(value):
    out = subprocess.run(
        ["bash", "-c", f"v={rc_quote(value, 'bash')}; printf %s \"$v\""],
        capture_output=True, check=True, env={"PATH": "/usr/bin:/bin"},
    ).stdout.decode()
    assert out == value


@pytest.mark.parametrize("uri,opened", [
    ("file:///etc/passwd", False),
    ("https://evil.example/device?code=Q", False),
    ("{server}/device?code=Q", True),
])
def test_verification_uri_complete_opened_only_on_server(home, lumen_server, tmp_path, uri, opened):
    port = lumen_server.server_address[1]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "opened"
    for name in ("open", "xdg-open"):
        fake = bindir / name
        fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> {log}\n')
        fake.chmod(0o755)
    lumen_server.auth_extra = {
        "verification_uri_complete": uri.format(server=f"http://127.0.0.1:{port}"),
    }
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"],
                   env_extra={"PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"})
    assert r.returncode == 0, r.stderr
    assert log.exists() == opened


def test_env_key_not_sent_to_different_server(home, lumen_server):
    """A key issued by server A must not leak when -s names server B: the
    mismatch forces a fresh login instead."""
    port = lumen_server.server_address[1]
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode", "-s", f"http://127.0.0.1:{port}"],
                   env_extra={"LUMEN_API_KEY": "sk_from_server_A_0000",
                              "LUMEN_BASE_URL": "https://lumen.example.edu"})
    # A login round trip against server B happened...
    assert r.returncode == 0, r.stderr
    assert lumen_server.hits.get("/oauth/device_authorization") == 1
    assert TEST_KEY in r.stdout.decode()
    # ...and server A's key was never used.
    assert "sk_from_server_A_0000" not in r.stdout.decode() + r.stderr.decode()


def test_env_key_with_default_server_still_works(home, lumen_server):
    """Hand-exported keys (LUMEN_BASE_URL unset) keep working: the :-fallback
    matches them against the default server."""
    r = run_script(home, None, args=["--no-opencode"],
                   env_extra={"LUMEN_API_KEY": "sk_hand_exported_0000"})
    assert r.returncode == 0, r.stderr
    assert "sk_hand_exported_0000" in r.stdout.decode()
    assert r.stdout.decode().count("sk_hand_exported_0000") == 1  # echoed, not refetched


# --- model sync --------------------------------------------------------------

def test_env_key_with_trailing_slash_base_url_matches(home, lumen_server):
    port = lumen_server.server_address[1]
    r = run_script(home, port, args=["--no-opencode"],
                   env_extra={"LUMEN_API_KEY": "sk_slash_ok_000000",
                              "LUMEN_BASE_URL": f"http://127.0.0.1:{port}/"})
    assert r.returncode == 0, r.stderr
    assert "/oauth/device_authorization" not in lumen_server.hits
    assert "sk_slash_ok_000000" in r.stdout.decode()


def test_sync_writes_provider_block(home, lumen_server):
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_stored_key_000"}))
    r = run_script(home, port)
    assert r.returncode == 0, r.stderr
    cfg = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    lumen = cfg["provider"]["lumen"]
    assert lumen["options"]["baseURL"] == f"http://127.0.0.1:{port}/v1"
    assert lumen["options"]["apiKey"] == "{env:LUMEN_API_KEY}"
    assert set(lumen["models"]) == {"gpt-demo", "gpt-alias"}
    assert "alias of gpt-demo" in lumen["models"]["gpt-alias"]["name"]
    assert cfg["enabled_providers"] == ["lumen"]
    assert b"Updated 2 models" in r.stdout


def test_sync_no_aliases_flag(home, lumen_server):
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_stored_key_000"}))
    r = run_script(home, port, args=["--no-aliases"])
    assert r.returncode == 0, r.stderr
    cfg = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    assert set(cfg["provider"]["lumen"]["models"]) == {"gpt-demo"}


def test_sync_writes_capability_flags(home, lumen_server):
    """Capability fields from /v1/models land in the config as OpenCode's
    modalities/attachment/tool_call/reasoning flags; models without the data
    get only tool_call: false, since OpenCode assumes tool calling unless
    told false (issue #79)."""
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_stored_key_000"}))
    r = run_script(home, port)
    assert r.returncode == 0, r.stderr
    models = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text()
    )["provider"]["lumen"]["models"]
    demo = models["gpt-demo"]
    assert demo["modalities"] == {"input": ["text", "image"], "output": ["text"]}
    assert demo["attachment"] is True
    assert demo["tool_call"] is True
    assert demo["reasoning"] is True
    alias = models["gpt-alias"]
    assert alias["modalities"] == {"output": ["text"]}
    assert "attachment" not in alias
    assert alias["tool_call"] is False
    assert "reasoning" not in alias


def test_sync_reports_added_and_removed(home, lumen_server):
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_stored_key_000"}))
    cfg_path = home / ".config" / "opencode" / "opencode.json"
    cfg_path.write_text(json.dumps({"provider": {"lumen": {"models": {"gone-model": {}}}}}))
    r = run_script(home, port)
    assert r.returncode == 0, r.stderr
    assert b"+ gpt-demo" in r.stdout
    assert b"- gone-model" in r.stdout


# --- hostile server values ----------------------------------------------------

def fake_bin(tmp_path, start=1_000_000, step=0):
    """Fake sleep (logs its argument, returns at once) and date (returns
    start, then advances by step on every later call) on a PATH prefix."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sleeps = tmp_path / "sleeps"
    clock = tmp_path / "clock"
    (bindir / "sleep").write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> {sleeps}\n')
    (bindir / "date").write_text(
        f'#!/bin/sh\nt=$(cat {clock} 2>/dev/null || echo {start - step})\n'
        f't=$((t + {step}))\necho $t > {clock}\necho $t\n'
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"}
    return env, sleeps


def logged_sleeps(sleeps):
    return [int(x) for x in sleeps.read_text().split()]


def test_interval_clamped(home, lumen_server, tmp_path):
    port = lumen_server.server_address[1]
    env, sleeps = fake_bin(tmp_path)
    lumen_server.auth_extra = {"interval": 999999999}
    lumen_server.token_queue = [
        (400, {"error": "slow_down"}),
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert max(logged_sleeps(sleeps)) == 60


def test_expires_in_clamped(home, lumen_server, tmp_path):
    """With expires_in clamped to 1800 s, a clock that jumps 1801 s per poll
    times out on the first poll instead of waiting out the server's value."""
    port = lumen_server.server_address[1]
    env, _ = fake_bin(tmp_path, step=1801)
    lumen_server.auth_extra = {"expires_in": 999999999}
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"], env_extra=env)
    assert r.returncode != 0
    assert b"timed out" in r.stderr
    assert "/oauth/token" not in lumen_server.hits


@pytest.mark.parametrize("retry_after,expected", [
    ("999999", 60),
    ("30", 30),
    ("Wed, 21 Oct 2026 07:28:00 GMT", 6),  # not a number: interval + 5
    ("$(id)", 6),
])
def test_retry_after_validated_and_clamped(home, lumen_server, tmp_path, retry_after, expected):
    port = lumen_server.server_address[1]
    env, sleeps = fake_bin(tmp_path)
    lumen_server.token_queue = [
        (429, {"error": "rate_limited"}, {"Retry-After": retry_after}),
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"], env_extra=env)
    assert r.returncode == 0, r.stderr
    assert expected in logged_sleeps(sleeps)
    assert max(logged_sleeps(sleeps)) <= 60


def test_server_text_control_characters_stripped(home, lumen_server):
    port = lumen_server.server_address[1]
    lumen_server.auth_extra = {
        "user_code": "QQQQ\x1b[2J-QQQQ",
        "verification_uri": "http://127.0.0.1:1/device\x1b]0;pwned\x07",
    }
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer",
               "approved_by": "alice\x1b[31m@example.edu\r\n"}),
    ]
    r = run_script(home, port, args=["--no-opencode"])
    assert r.returncode == 0, r.stderr
    out = r.stdout.decode()
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out
    assert "Approved by alice[31m@example.edu." in out
    assert "enter code QQQQ[2J-QQQQ" in out


def test_malformed_stored_key_ignored(home, lumen_server):
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_bad$(id)"}))
    lumen_server.token_queue = [
        (200, {"access_token": TEST_KEY, "token_type": "Bearer", "approved_by": "a@b"}),
    ]
    r = run_script(home, port, args=["--no-opencode"])
    assert r.returncode == 0, r.stderr
    assert b"ignoring malformed key" in r.stderr
    assert lumen_server.hits.get("/oauth/device_authorization") == 1
    assert json.loads(keys.read_text())[host_key(port)] == TEST_KEY
    assert "sk_bad" not in r.stdout.decode()


def test_malformed_env_key_ignored(home, lumen_server):
    port = lumen_server.server_address[1]
    keys = home / ".config" / "lumen" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({host_key(port): "sk_stored_key_000"}))
    r = run_script(home, port, args=["--no-opencode"],
                   env_extra={"LUMEN_API_KEY": "not a key"})
    assert r.returncode == 0, r.stderr
    assert b"ignoring malformed LUMEN_API_KEY" in r.stderr
    assert "LUMEN_API_KEY=sk_stored_key_000" in r.stdout.decode()
