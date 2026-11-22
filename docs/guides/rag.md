# Ask Questions About Your Files (RAG)

`scripts/rag.py` in the Lumen repository is a small retrieval-augmented generation (RAG) helper. It splits your documents into chunks, embeds them through Lumen's [`/v1/embeddings`](api-reference.md) endpoint, stores the vectors in a local SQLite file, and answers questions by sending the best-matching chunks to a chat model. Everything is billed to your API key like any other request.

It is a single script with one dependency (`pypdf`); `uv` installs that for you.

## Requirements

- An API key and the server URL. Run `./lumen.sh --relogin` from the repository root (see [Connect Your Tools](connect.md)), or export them yourself:

  ```bash
  export LUMEN_BASE_URL=https://your-lumen-server
  export LUMEN_API_KEY=your-api-key
  ```

- An embedding model and a chat model you can use. Pick them on the [Models](../models/models.md) page; embedding models are marked as such and do not appear in chat.

## Configuration

Each setting is read from the environment first, then from the `[lumen]` section of `~/.config/lumen/config.ini` (`$LUMEN_CONFIG_DIR/config.ini` when that variable is set). `lumen.sh` writes `base_url` and `api_key` at login; add the models by hand.

| Environment variable | `config.ini` key | Default | Purpose |
|---|---|---|---|
| `LUMEN_BASE_URL` | `base_url` | – (required) | Lumen server, e.g. `http://localhost:5001` |
| `LUMEN_API_KEY` | `api_key` | – (required) | Your API key |
| `LUMEN_EMBED_MODEL` | `embed_model` | `qwen3-embedding-8b` | Model used to embed chunks and questions |
| `LUMEN_CHAT_MODEL` | `chat_model` | `qwen3.8-27b` | Model that writes the answer |
| `LUMEN_VERBOSE` | – | off | Set to `1` to print each step; same as `-v` |

The default model names are the ones on a single Lumen deployment, so you will normally set both for your server. If the server does not know a model, the script says which setting to change.

```ini
[lumen]
embed_model = your-embedding-model
chat_model = your-chat-model
```

## Usage

```bash
./scripts/rag.py add report.pdf            # embed and store a file
./scripts/rag.py add notes.md
./scripts/rag.py query "What were the main findings?"
./scripts/rag.py list                      # show stored files
./scripts/rag.py delete report.pdf         # also: remove, rm
./scripts/rag.py clear                     # delete everything (asks first)
```

| Command | What it does |
|---|---|
| `add <file>` | Extracts the text (`.pdf` via pypdf; anything else is read as UTF-8 text, e.g. `.txt` and `.md`), splits it into chunks, embeds them and stores them. Adding a file again replaces its earlier chunks. |
| `query <question>` | Embeds the question, picks the 5 most similar chunks (cosine similarity) and asks the chat model to answer from them only. Prints the answer and the file names it used. |
| `list` | Lists each stored file with its chunk count and vector size. |
| `delete <file>` | Removes a file. Give the path you added, or just the file name; if the name matches several stored files, the script lists them and stops unless you pass the full path or `-y`/`--yes`. |
| `clear` | Deletes every chunk. Asks for confirmation unless you pass `-y`/`--yes`. |

Options that work with every command:

| Option | Purpose |
|---|---|
| `-f`, `--file DB` | Use a different SQLite store instead of `store.db` in the config directory. Keep one store per topic to avoid mixing documents. |
| `-v`, `--verbose` | Print each step, including the chunks that matched and their scores. |

## How it works

- Text is cut into chunks of about 1,000 characters that overlap by 200, so a sentence at a boundary appears in both neighbours.
- Chunks are embedded in batches of 8. Transient errors (HTTP 429, 502, 503, 504, timeouts) are retried up to 3 times.
- The store is one SQLite table. A query loads every chunk and ranks it in Python, which is fine for a few dozen documents but not meant for large collections.
- The chat model is told to answer only from the retrieved context and to name its sources. If the answer is not in your files, expect it to say so.

## Tips and limits

- Scanned PDFs without a text layer have nothing to extract; the script reports "No extractable text".
- Embeddings from different models are not comparable. If you change `embed_model`, `clear` the store and `add` your files again.
- Chunk and result sizes are constants at the top of the script (`CHUNK_SIZE`, `CHUNK_OVERLAP`, `TOP_K`); edit them to tune recall.
- The store holds your document text in plain form. A new store and any directory the script creates for it are private to you (modes 700 and 600); an existing store keeps its permissions, so check them if you created it with an older version. Do not add documents you are not allowed to send to the models on your Lumen server.
