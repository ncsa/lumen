#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pypdf>=6.21.4"]
# ///
"""Small RAG helper for Lumen.

Usage (run directly; uv resolves dependencies via the script header).
Add -f/--file <db> to any command to use a different SQLite store
(default: store.db in the config directory, see below):

    ./scripts/rag.py add <file.pdf|file.txt|file.md>
    ./scripts/rag.py query "your question"
    ./scripts/rag.py list
    ./scripts/rag.py delete|remove|rm <file>
    ./scripts/rag.py clear [--yes]

Configuration: environment variables win over the [lumen] section of
$LUMEN_CONFIG_DIR/config.ini (default ~/.config/lumen/config.ini), which
./lumen.sh writes at login (base_url, api_key); the models are added by hand:
    LUMEN_BASE_URL    (base_url)     required, e.g. http://localhost:5001
    LUMEN_API_KEY     (api_key)      required (run ./lumen.sh --relogin)
    LUMEN_EMBED_MODEL (embed_model)  default qwen3-embedding-8b
    LUMEN_CHAT_MODEL  (chat_model)   default qwen3.8-27b
"""

import argparse
import configparser
import heapq
import json
import math
import os
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
import warnings
from collections import defaultdict
from contextlib import closing
from http import HTTPStatus
from pathlib import Path

from pypdf import PdfReader

warnings.filterwarnings("ignore")

CONFIG_DIR = Path(os.environ.get("LUMEN_CONFIG_DIR") or Path.home() / ".config" / "lumen")
CONFIG_FILE = CONFIG_DIR / "config.ini"
# interpolation=None: a "%" in a key or URL is a literal character.
_config = configparser.ConfigParser(interpolation=None)
try:
    _config.read(CONFIG_FILE, encoding="utf-8")  # a missing file is fine
except (configparser.Error, UnicodeDecodeError) as _e:
    sys.exit(f"Error: cannot read {CONFIG_FILE}: {_e}")


def _setting(env_name, key, default=""):
    """Environment variable first, then [lumen] <key> in config.ini."""
    return os.environ.get(env_name) or _config.get("lumen", key, fallback=default)


BASE_URL = _setting("LUMEN_BASE_URL", "base_url").rstrip("/")
API_KEY = _setting("LUMEN_API_KEY", "api_key")
EMBED_MODEL = _setting("LUMEN_EMBED_MODEL", "embed_model", "qwen3-embedding-8b")
CHAT_MODEL = _setting("LUMEN_CHAT_MODEL", "chat_model", "qwen3.8-27b")

DB_PATH = CONFIG_DIR / "store.db"
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
BATCH_SIZE = 8
TOP_K = 5
REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_STATUSES = {HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.BAD_GATEWAY,
                  HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.GATEWAY_TIMEOUT}
VERBOSE = os.environ.get("LUMEN_VERBOSE", "").lower() in ("1", "true", "yes")


def _request(path, payload):
    """POST JSON to Lumen; retry transient failures, raise RuntimeError otherwise."""
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            BASE_URL + path,
            data=body,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            e.close()
            last = RuntimeError(f"HTTP {e.code} from {path}: {detail}")
            if e.code not in RETRY_STATUSES:
                raise last from e
        except (urllib.error.URLError, TimeoutError) as e:
            last = RuntimeError(f"Request to {BASE_URL}{path} failed: {e}")
        if attempt < MAX_RETRIES - 1:
            time.sleep(2**attempt)
    raise last or RuntimeError(f"No request attempted: MAX_RETRIES is {MAX_RETRIES}")


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            chunk_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL
        )"""
    )
    conn.commit()
    return conn


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob):
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def load_chunks(conn):
    out = []
    for source, chunk_id, text, blob in conn.execute(
        "SELECT source, chunk_id, text, embedding FROM chunks"
    ):
        out.append(
            {
                "source": source,
                "chunk_id": chunk_id,
                "text": text,
                "embedding": _unpack(blob),
            }
        )
    return out


def extract_text(path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(p))
        return "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
    return p.read_text(encoding="utf-8", errors="replace")


def chunk_text(text):
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def embed_texts(texts):
    if not texts:
        return []
    vectors = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        resp = _request("/v1/embeddings", {"model": EMBED_MODEL, "input": batch})
        try:
            data = sorted(resp["data"], key=lambda d: d["index"])
            batch_vectors = [d["embedding"] for d in data]
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Unexpected embeddings response: {str(resp)[:500]}") from e
        if len(batch_vectors) != len(batch):
            raise RuntimeError(
                f"Embeddings response has {len(batch_vectors)} vectors for {len(batch)} inputs"
            )
        vectors.extend(batch_vectors)
    return vectors


def cosine(a, b, na=None):
    """Cosine similarity; pass the precomputed norm of `a` as `na` to reuse it."""
    dot = sum(x * y for x, y in zip(a, b))
    if na is None:
        na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def chat(messages):
    resp = _request(
        "/v1/chat/completions",
        {
            "model": CHAT_MODEL,
            "messages": messages,
            "max_tokens": 1500,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected chat response: {str(resp)[:500]}") from e
    content = (msg.get("content") or "").strip()
    if not content:
        raise RuntimeError("The chat model returned an empty answer.")
    return content


def cmd_add(path):
    src = str(Path(path).resolve())
    if VERBOSE:
        print(f"[verbose] Resolved source: {src}")
        print(f"[verbose] Extracting text from {path} ...")
    text = extract_text(path)
    if VERBOSE:
        print(f"[verbose] Extracted {len(text)} characters.")
        print("[verbose] Chunking text ...")
    chunks = chunk_text(text)
    if not chunks:
        print(f"No extractable text in {path}")
        return
    if VERBOSE:
        print(f"[verbose] Produced {len(chunks)} chunks.")
        print(f"[verbose] Embedding {len(chunks)} chunks via {EMBED_MODEL} ...")
    else:
        print(f"Embedding {len(chunks)} chunks from {path} ...")
    vectors = embed_texts(chunks)
    if VERBOSE:
        print("[verbose] Storing chunks in SQLite ...")
    with closing(get_conn()) as conn:
        conn.execute("DELETE FROM chunks WHERE source = ?", (src,))
        for i, (t, v) in enumerate(zip(chunks, vectors)):
            conn.execute(
                "INSERT INTO chunks (source, chunk_id, text, embedding) VALUES (?,?,?,?)",
                (src, i, t, _pack(v)),
            )
        conn.commit()
    print(f"Added {path}: chunks={len(chunks)}, vector_dim={len(vectors[0])}")


def cmd_delete(path, force=False):
    src = str(Path(path).resolve())
    with closing(get_conn()) as conn:
        if VERBOSE:
            print(f"[verbose] Deleting chunks for source: {src}")
        cur = conn.execute("DELETE FROM chunks WHERE source = ?", (src,))
        n = cur.rowcount
        if n == 0:
            if VERBOSE:
                print("[verbose] No exact match; trying basename match ...")
            name = Path(path).name
            sources = [
                r[0]
                for r in conn.execute("SELECT DISTINCT source FROM chunks")
                if Path(r[0]).name == name
            ]
            if len(sources) > 1 and not force:
                print(f"{path} matches {len(sources)} stored files; pass the full path or --yes:")
                for source in sources:
                    print(f"    {source}")
                conn.rollback()
                return
            for source in sources:
                n += conn.execute("DELETE FROM chunks WHERE source = ?", (source,)).rowcount
        conn.commit()
    if n:
        print(f"Removed {path}: {n} chunks deleted.")
    else:
        print(f"No chunks found for {path}.")


def cmd_clear(force=False):
    with closing(get_conn()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if total == 0:
            print("Store is already empty.")
            return
        if not force:
            ans = input(f"Delete all {total} chunks? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("Aborted.")
                return
        conn.execute("DELETE FROM chunks")
        conn.commit()
    print(f"Cleared store: {total} chunks deleted.")


def cmd_list():
    with closing(get_conn()) as conn:
        chunks = load_chunks(conn)
    groups = defaultdict(list)
    for c in chunks:
        groups[c["source"]].append(c)
    if not groups:
        print("No files added yet.")
        return
    for src, cs in groups.items():
        print(
            f"{Path(src).name}  (chunks={len(cs)}, vector_dim={len(cs[0]['embedding'])})"
        )
        print(f"    {src}")
    print(f"Total chunks: {len(chunks)}")


def cmd_query(question):
    with closing(get_conn()) as conn:
        chunks = load_chunks(conn)
    if not chunks:
        print("No data. Add files first with: rag.py add <file>")
        return
    if VERBOSE:
        print(f"[verbose] Loaded {len(chunks)} chunks from store.")
        print(f"[verbose] Embedding question via {EMBED_MODEL} ...")
    qvec = embed_texts([question])[0]
    if VERBOSE:
        print(f"[verbose] Computing similarity over {len(chunks)} chunks ...")
    qnorm = math.sqrt(sum(x * x for x in qvec))
    # Rank on the score only: equal scores must never fall through to comparing dicts.
    scored = ((cosine(qvec, c["embedding"], qnorm), c) for c in chunks)
    top = heapq.nlargest(TOP_K, scored, key=lambda t: t[0])
    if VERBOSE:
        print(f"[verbose] Top {len(top)} matching chunks:")
        for sim, c in top:
            print(f"    {sim:.4f}  {Path(c['source']).name} (chunk {c['chunk_id']})")
        print(f"[verbose] Querying LLM {CHAT_MODEL} ...")
    context = "\n\n".join(
        f"[Source: {Path(c['source']).name}]\n{c['text']}" for _, c in top
    )
    used = sorted({Path(c["source"]).name for _, c in top})
    messages = [
        {
            "role": "system",
            "content": (
                "You answer questions using only the provided context. "
                "Cite the source file names you relied on."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Context:\n{context}\n\nQuestion: {question}\n\n"
                "Answer the question and state which files you used."
            ),
        },
    ]
    answer = chat(messages)
    print(answer)
    print("\nFiles used: " + ", ".join(used))


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS: a subparser must not overwrite a value given before the subcommand.
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="verbose output",
    )
    common.add_argument(
        "-f", "--file", dest="db", metavar="DB", default=argparse.SUPPRESS,
        help=f"SQLite store to use (default: {DB_PATH})",
    )
    parser = argparse.ArgumentParser(
        prog="rag.py", description="Small RAG helper for Lumen.", parents=[common]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", parents=[common], help="embed and store a file")
    p.add_argument("file")
    p = sub.add_parser("query", parents=[common], help="ask a question")
    p.add_argument("question", nargs="+")
    sub.add_parser("list", parents=[common], help="list stored files")
    p = sub.add_parser(
        "delete", aliases=["remove", "rm"], parents=[common], help="remove a file"
    )
    p.add_argument("file")
    p.add_argument("-y", "--yes", action="store_true", help="delete every basename match")
    p = sub.add_parser("clear", parents=[common], help="delete everything")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    return parser


def main(argv=None):
    global VERBOSE, DB_PATH
    args = build_parser().parse_args(argv)
    if not BASE_URL or not API_KEY:
        print("Error: LUMEN_BASE_URL and LUMEN_API_KEY must be set.")
        print(f"Run ./lumen.sh --relogin (from the repo root) to store them in {CONFIG_FILE},")
        print("or export them manually:")
        print("  export LUMEN_BASE_URL=http://localhost:5001")
        print("  export LUMEN_API_KEY=your-api-key")
        sys.exit(1)
    if getattr(args, "verbose", False):
        VERBOSE = True
    if getattr(args, "db", None):
        DB_PATH = Path(args.db).expanduser().resolve()
    try:
        if args.cmd == "add":
            cmd_add(args.file)
        elif args.cmd == "query":
            cmd_query(" ".join(args.question))
        elif args.cmd == "list":
            cmd_list()
        elif args.cmd in ("delete", "remove", "rm"):
            cmd_delete(args.file, force=args.yes)
        elif args.cmd == "clear":
            cmd_clear(force=args.yes)
    except (RuntimeError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
