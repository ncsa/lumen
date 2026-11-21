"""Tests for scripts/rag.py (the small RAG helper), loaded by path."""
import importlib.util
import io
import json
import urllib.error
import warnings
from contextlib import closing
from http import HTTPStatus
from pathlib import Path

import pytest
from pypdf import PdfWriter

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rag.py"

ENV_NAMES = ("LUMEN_BASE_URL", "LUMEN_API_KEY", "LUMEN_EMBED_MODEL", "LUMEN_CHAT_MODEL",
             "LUMEN_VERBOSE")


def load_rag(monkeypatch, config_dir, ini=None, env=None):
    """Import a fresh copy of rag.py with LUMEN_CONFIG_DIR pointing at config_dir."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LUMEN_CONFIG_DIR", str(config_dir))
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    if ini is not None:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.ini").write_text(ini)
    spec = importlib.util.spec_from_file_location("rag_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # rag.py silences all warnings at import; keep that out of the test session.
    with warnings.catch_warnings():
        spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rag(monkeypatch, tmp_path):
    module = load_rag(monkeypatch, tmp_path / "cfg",
                      env={"LUMEN_BASE_URL": "http://lumen.test", "LUMEN_API_KEY": "sk_test"})
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return module


@pytest.fixture()
def fake_api(rag, monkeypatch):
    """Replace the two API calls: every text embeds to [1, 0] unless the text says
    otherwise, and the chat model answers with a fixed string."""
    calls = {"embed": [], "chat": []}

    def embed(texts):
        calls["embed"].append(list(texts))
        return [[0.0, 1.0] if "other" in t else [1.0, 0.0] for t in texts]

    def chat(messages):
        calls["chat"].append(messages)
        return "the answer"

    monkeypatch.setattr(rag, "embed_texts", embed)
    monkeypatch.setattr(rag, "chat", chat)
    return calls


def run(rag, *argv):
    rag.main(list(argv))


# --- configuration --------------------------------------------------------------

def test_settings_come_from_config_ini(monkeypatch, tmp_path):
    m = load_rag(monkeypatch, tmp_path / "cfg", ini=(
        "[lumen]\nbase_url = http://ini.test/\napi_key = sk_ini%x\n"
        "embed_model = emb-ini\nchat_model = chat-ini\n"))
    assert (m.BASE_URL, m.API_KEY) == ("http://ini.test", "sk_ini%x")
    assert (m.EMBED_MODEL, m.CHAT_MODEL) == ("emb-ini", "chat-ini")
    assert m.DB_PATH == tmp_path / "cfg" / "store.db"


def test_environment_wins_over_config_ini(monkeypatch, tmp_path):
    m = load_rag(monkeypatch, tmp_path / "cfg",
                 ini="[lumen]\nbase_url = http://ini.test\nchat_model = chat-ini\n",
                 env={"LUMEN_BASE_URL": "http://env.test", "LUMEN_CHAT_MODEL": "chat-env"})
    assert (m.BASE_URL, m.CHAT_MODEL) == ("http://env.test", "chat-env")


def test_defaults_without_any_configuration(monkeypatch, tmp_path):
    m = load_rag(monkeypatch, tmp_path / "missing")
    assert (m.BASE_URL, m.API_KEY) == ("", "")
    assert (m.EMBED_MODEL, m.CHAT_MODEL) == ("qwen3-embedding-8b", "qwen3.8-27b")


def test_malformed_config_ini_exits_with_message(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as e:
        load_rag(monkeypatch, tmp_path / "cfg", ini="no section header\n")
    assert "config.ini" in str(e.value)


def test_missing_credentials_exit_with_help(rag, monkeypatch, capsys):
    monkeypatch.setattr(rag, "BASE_URL", "")
    with pytest.raises(SystemExit) as e:
        run(rag, "list")
    assert e.value.code == 1
    assert "LUMEN_BASE_URL and LUMEN_API_KEY must be set" in capsys.readouterr().out


# --- pure helpers -----------------------------------------------------------------

def test_pack_unpack_round_trip(rag):
    assert rag._unpack(rag._pack([0.5, -2.0, 3.25])) == [0.5, -2.0, 3.25]


def test_cosine(rag):
    assert rag.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert rag.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert rag.cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert rag.cosine([0, 0], [1, 0]) == 0.0
    assert rag.cosine([1, 0], [0, 0]) == 0.0
    assert rag.cosine([3, 4], [3, 4], na=5.0) == pytest.approx(1.0)


def test_chunk_text_empty(rag):
    assert rag.chunk_text("") == []
    assert rag.chunk_text(None) == []
    assert rag.chunk_text("  \n ") == []


def test_chunk_text_overlaps(rag, monkeypatch):
    monkeypatch.setattr(rag, "CHUNK_SIZE", 10)
    monkeypatch.setattr(rag, "CHUNK_OVERLAP", 3)
    text = "abcdefghijklmnopqrstuvwxyz"
    chunks = rag.chunk_text(text)
    assert chunks[0] == text[:10]
    assert chunks[1] == text[7:17]
    assert chunks[-1].endswith("z")
    assert all(len(c) <= 10 for c in chunks)


def test_chunk_text_terminates_when_overlap_is_not_smaller_than_size(rag, monkeypatch):
    monkeypatch.setattr(rag, "CHUNK_SIZE", 10)
    monkeypatch.setattr(rag, "CHUNK_OVERLAP", 10)
    chunks = rag.chunk_text("x" * 30)
    assert chunks and chunks[-1].endswith("x")


def test_extract_text_plain_and_missing(rag, tmp_path):
    f = tmp_path / "a.md"
    f.write_text("hello")
    assert rag.extract_text(f) == "hello"
    with pytest.raises(FileNotFoundError):
        rag.extract_text(tmp_path / "nope.txt")


def test_extract_text_pdf(rag, tmp_path):
    f = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with f.open("wb") as fh:
        writer.write(fh)
    assert rag.extract_text(f).strip() == ""


# --- HTTP ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def http_error(code, body=b"boom"):
    return urllib.error.HTTPError("http://lumen.test/x", code, "msg", {}, io.BytesIO(body))


def test_request_sends_bearer_json_and_timeout(rag, monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"),
                    body=json.loads(req.data), timeout=timeout, method=req.get_method())
        return FakeResponse({"ok": True})

    monkeypatch.setattr(rag.urllib.request, "urlopen", urlopen)
    assert rag._request("/v1/x", {"a": 1}) == {"ok": True}
    assert seen == {"url": "http://lumen.test/v1/x", "auth": "Bearer sk_test",
                    "body": {"a": 1}, "timeout": rag.REQUEST_TIMEOUT, "method": "POST"}


def test_request_retries_transient_errors(rag, monkeypatch):
    outcomes = [http_error(HTTPStatus.TOO_MANY_REQUESTS), urllib.error.URLError("down"),
                FakeResponse({"ok": 1})]

    def urlopen(req, timeout):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(rag.urllib.request, "urlopen", urlopen)
    assert rag._request("/v1/x", {}) == {"ok": 1}
    assert outcomes == []


def test_request_gives_up_after_max_retries(rag, monkeypatch):
    attempts = []

    def urlopen(req, timeout):
        attempts.append(1)
        raise http_error(HTTPStatus.SERVICE_UNAVAILABLE)

    monkeypatch.setattr(rag.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        rag._request("/v1/x", {})
    assert len(attempts) == rag.MAX_RETRIES


def test_request_does_not_retry_client_errors(rag, monkeypatch):
    attempts = []

    def urlopen(req, timeout):
        attempts.append(1)
        raise http_error(HTTPStatus.UNAUTHORIZED, b"bad key")

    monkeypatch.setattr(rag.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="HTTP 401.*bad key"):
        rag._request("/v1/x", {})
    assert len(attempts) == 1


def test_request_reports_unreachable_server(rag, monkeypatch):
    def urlopen(req, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(rag.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="failed: .*refused"):
        rag._request("/v1/x", {})


def test_embed_texts_batches_and_orders_by_index(rag, monkeypatch):
    monkeypatch.setattr(rag, "BATCH_SIZE", 2)
    batches = []

    def request(path, payload):
        assert path == "/v1/embeddings"
        batches.append(payload["input"])
        # Answer out of order: embed_texts must sort by index.
        return {"data": [{"index": i, "embedding": [float(len(t))]}
                         for i, t in reversed(list(enumerate(payload["input"])))]}

    monkeypatch.setattr(rag, "_request", request)
    assert rag.embed_texts(["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert batches == [["a", "bb"], ["ccc"]]
    assert rag.embed_texts([]) == []


def test_embed_texts_rejects_bad_payloads(rag, monkeypatch):
    monkeypatch.setattr(rag, "_request", lambda p, d: {"error": "nope"})
    with pytest.raises(RuntimeError, match="Unexpected embeddings response"):
        rag.embed_texts(["a"])
    monkeypatch.setattr(rag, "_request", lambda p, d: {"data": [{"index": 0, "embedding": [1.0]}]})
    with pytest.raises(RuntimeError, match="1 vectors for 2 inputs"):
        rag.embed_texts(["a", "b"])


def test_chat_returns_content_and_sends_model(rag, monkeypatch):
    sent = {}

    def request(path, payload):
        sent.update(path=path, payload=payload)
        return {"choices": [{"message": {"content": "  hi  "}}]}

    monkeypatch.setattr(rag, "_request", request)
    assert rag.chat([{"role": "user", "content": "q"}]) == "hi"
    assert sent["path"] == "/v1/chat/completions"
    assert sent["payload"]["model"] == rag.CHAT_MODEL


def test_chat_rejects_empty_answer_and_bad_shape(rag, monkeypatch):
    monkeypatch.setattr(rag, "_request", lambda p, d: {
        "choices": [{"message": {"content": "", "reasoning_content": "thinking"}}]})
    with pytest.raises(RuntimeError, match="empty answer"):
        rag.chat([])
    monkeypatch.setattr(rag, "_request", lambda p, d: {"choices": []})
    with pytest.raises(RuntimeError, match="Unexpected chat response"):
        rag.chat([])


# --- commands -----------------------------------------------------------------------

def make_file(tmp_path, name, text="hello world " * 5):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def stored_sources(rag):
    with closing(rag.get_conn()) as conn:
        return sorted(r[0] for r in conn.execute("SELECT DISTINCT source FROM chunks"))


def test_add_list_and_replace(rag, fake_api, tmp_path, capsys):
    f = make_file(tmp_path, "notes.txt")
    run(rag, "add", str(f))
    assert "Added" in capsys.readouterr().out
    run(rag, "add", str(f))  # re-adding replaces, it does not duplicate
    capsys.readouterr()
    run(rag, "list")
    out = capsys.readouterr().out
    assert "notes.txt  (chunks=1, vector_dim=2)" in out
    assert "Total chunks: 1" in out


def test_add_without_text(rag, fake_api, tmp_path, capsys):
    f = make_file(tmp_path, "empty.txt", "  ")
    run(rag, "add", str(f))
    assert "No extractable text" in capsys.readouterr().out
    assert fake_api["embed"] == []


def test_list_empty_store(rag, capsys):
    run(rag, "list")
    assert "No files added yet." in capsys.readouterr().out


def test_query_ranks_by_similarity_and_cites_files(rag, fake_api, tmp_path, capsys):
    run(rag, "add", str(make_file(tmp_path, "near.txt", "near text")))
    run(rag, "add", str(make_file(tmp_path, "far.txt", "other text")))
    capsys.readouterr()
    run(rag, "query", "what", "is", "near?")
    out = capsys.readouterr().out
    assert "the answer" in out
    assert "Files used: far.txt, near.txt" in out
    assert fake_api["embed"][-1] == ["what is near?"]
    context = fake_api["chat"][0][1]["content"]
    assert context.index("near text") < context.index("other text")  # best match first


def test_query_with_tied_scores(rag, fake_api, tmp_path, capsys):
    for name in ("a.txt", "b.txt", "c.txt"):
        run(rag, "add", str(make_file(tmp_path, name, "same text")))
    capsys.readouterr()
    run(rag, "query", "q")  # equal scores must not fall through to comparing chunks
    assert "the answer" in capsys.readouterr().out


def test_query_empty_store(rag, fake_api, capsys):
    run(rag, "query", "q")
    assert "No data" in capsys.readouterr().out
    assert fake_api["chat"] == []


def test_query_verbose_lists_matches(rag, fake_api, tmp_path, capsys):
    run(rag, "add", str(make_file(tmp_path, "a.txt")))
    capsys.readouterr()
    run(rag, "-v", "query", "q")
    out = capsys.readouterr().out
    assert "[verbose] Top 1 matching chunks" in out and "a.txt (chunk 0)" in out


def test_delete_exact_path(rag, fake_api, tmp_path, capsys):
    f = make_file(tmp_path, "a.txt")
    run(rag, "add", str(f))
    capsys.readouterr()
    run(rag, "rm", str(f))
    assert "1 chunks deleted" in capsys.readouterr().out
    assert stored_sources(rag) == []


def test_delete_by_basename_matches_whole_name_only(rag, fake_api, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(rag, "add", str(make_file(tmp_path, "x/data_a.txt")))
    run(rag, "add", str(make_file(tmp_path, "y/a.txt")))
    capsys.readouterr()
    run(rag, "delete", "a.txt")
    assert "Removed a.txt: 1 chunks deleted." in capsys.readouterr().out
    assert [Path(s).name for s in stored_sources(rag)] == ["data_a.txt"]


def test_delete_ambiguous_basename_needs_yes(rag, fake_api, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(rag, "add", str(make_file(tmp_path, "x/a.txt")))
    run(rag, "add", str(make_file(tmp_path, "y/a.txt")))
    capsys.readouterr()
    run(rag, "delete", "a.txt")
    assert "matches 2 stored files" in capsys.readouterr().out
    assert len(stored_sources(rag)) == 2
    run(rag, "delete", "--yes", "a.txt")
    assert "2 chunks deleted" in capsys.readouterr().out
    assert stored_sources(rag) == []


def test_delete_unknown_file(rag, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    run(rag, "delete", "ghost.txt")
    assert "No chunks found" in capsys.readouterr().out


def test_clear_confirmation(rag, fake_api, tmp_path, capsys, monkeypatch):
    run(rag, "clear")
    assert "already empty" in capsys.readouterr().out
    run(rag, "add", str(make_file(tmp_path, "a.txt")))
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    run(rag, "clear")
    assert "Aborted." in capsys.readouterr().out
    assert len(stored_sources(rag)) == 1
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    run(rag, "clear")
    assert "Cleared store: 1 chunks deleted." in capsys.readouterr().out
    assert stored_sources(rag) == []


def test_clear_yes_skips_prompt(rag, fake_api, tmp_path, capsys, monkeypatch):
    run(rag, "add", str(make_file(tmp_path, "a.txt")))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("must not prompt"))
    run(rag, "clear", "-y")
    assert stored_sources(rag) == []


# --- command line ---------------------------------------------------------------------

def test_file_option_selects_the_store_before_or_after_the_command(rag, fake_api, tmp_path, capsys):
    db = tmp_path / "other" / "mine.db"
    db.parent.mkdir()
    f = make_file(tmp_path, "a.txt")
    run(rag, "-f", str(db), "add", str(f))
    assert db.exists()
    assert not (tmp_path / "cfg" / "store.db").exists()
    capsys.readouterr()
    run(rag, "list", "--file", str(db))
    assert "a.txt" in capsys.readouterr().out


def test_query_words_may_look_like_flags_after_double_dash(rag):
    args = rag.build_parser().parse_args(["query", "--", "foo", "-y", "bar"])
    assert args.question == ["foo", "-y", "bar"]


def test_verbose_flag_works_before_the_subcommand(rag, fake_api, tmp_path, capsys):
    run(rag, "-v", "add", str(make_file(tmp_path, "a.txt")))
    assert "[verbose]" in capsys.readouterr().out


def test_errors_print_a_message_and_exit_1(rag, monkeypatch, capsys):
    def boom(*args):
        raise RuntimeError("server said no")

    monkeypatch.setattr(rag, "embed_texts", boom)
    with pytest.raises(SystemExit) as e:
        run(rag, "add", str(SCRIPT))
    assert e.value.code == 1
    assert "Error: server said no" in capsys.readouterr().err


def test_missing_file_is_an_error_not_a_traceback(rag, capsys, tmp_path):
    with pytest.raises(SystemExit) as e:
        run(rag, "add", str(tmp_path / "nope.txt"))
    assert e.value.code == 1
    assert "nope.txt" in capsys.readouterr().err


def test_unknown_command_is_rejected(rag):
    with pytest.raises(SystemExit) as e:
        run(rag, "frobnicate")
    assert e.value.code == 2
