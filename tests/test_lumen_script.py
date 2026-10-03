"""End-to-end tests for lumen.sh against a fake Lumen server."""
import json
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
            })
        elif self.path == "/oauth/token":
            assert form["grant_type"][0] == "urn:ietf:params:oauth:grant-type:device_code"
            code, obj = s.token_queue.pop(0) if s.token_queue else (400, {"error": "expired_token"})
            self._json(code, obj)
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


def test_empty_path_segment_rejected():
    r = run_script(None, "https://x.example.edu//api", args=["--no-opencode"])
    assert r.returncode != 0
    assert b"empty segments" in r.stderr


# --- device flow ------------------------------------------------------------

def test_device_flow_stores_key_and_rc_block(home, lumen_server):
    port = lumen_server.server_address[1]
    lumen_server.token_queue = [
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": TEST_KEY, "token_type": "Bearer",
               "approved_by": "alice@example.edu"}),
    ]
    r = run_script(home, port, args=["--no-opencode"], timeout=25)
    assert r.returncode == 0, r.stderr
    assert b"QQQQ-QQQQ" in r.stdout
    assert b"alice@example.edu" in r.stdout
    keys = json.loads((home / ".config" / "lumen" / "keys.json").read_text())
    assert keys[host_key(port)] == TEST_KEY
    bashrc = (home / ".bashrc").read_text()
    assert "# >>> lumen >>>" in bashrc
    assert f'export LUMEN_API_KEY="{TEST_KEY}"' in bashrc
    assert f'export LUMEN_BASE_URL="http://127.0.0.1:{port}"' in bashrc
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
    assert f'export LUMEN_API_KEY="{TEST_KEY}"' in bashrc
    assert "sk_old_key" not in bashrc


@pytest.mark.parametrize("shell_name,rc_file,line_fmt", [
    ("zsh", ".zshrc", 'export LUMEN_{k}="{v}"'),
    ("csh", ".cshrc", 'setenv LUMEN_{k} "{v}"'),
    ("fish", ".config/fish/config.fish", 'set -gx LUMEN_{k} "{v}"'),
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
    get no capability keys at all (issue #79)."""
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
    assert "tool_call" not in alias
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
