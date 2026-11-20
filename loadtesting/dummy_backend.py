"""Dummy OpenAI-compatible backend for load testing Lumen without hitting real models."""
import base64
import json
import struct
import time

from flask import Flask, Response, jsonify, request

app = Flask(__name__)


@app.get("/v1/models")
def list_models():
    return jsonify({
        "object": "list",
        "data": [{"id": "dummy", "object": "model", "created": 0, "owned_by": "local"}],
    })


@app.post("/v1/chat/completions")
def chat_completions():
    messages = request.json.get("messages", [])
    stream = request.json.get("stream", False)
    last_msg = messages[-1]["content"] if messages else "(empty)"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    reply = (
        f"**Echo** ({now}):\n\n> {last_msg}\n\n"
        f"Here is some inline math: $x^2 + y^2 = z^2$\n\n"
        f"And a display equation:\n\n"
        f"$$S = \\sum_{{i=1}}^{{n}} \\sum_{{j=1}}^{{i}} f(i,j)$$\n"
    )
    prompt_tokens = sum(len(m.get("content", "")) // 4 for m in messages)
    completion_tokens = len(reply) // 4

    if stream:
        def generate():
            cid = "chatcmpl-dummy"
            created = int(time.time())
            # Stream reply word by word with a small delay
            words = reply.split(" ")
            for i, word in enumerate(words):
                text = word + (" " if i < len(words) - 1 else "")
                chunk = {
                    "id": cid, "object": "chat.completion.chunk",
                    "created": created, "model": "dummy",
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                time.sleep(0.02)
            # Final chunk with usage
            final = {
                "id": cid, "object": "chat.completion.chunk",
                "created": created, "model": "dummy",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                          "total_tokens": prompt_tokens + completion_tokens},
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return Response(generate(), content_type="text/event-stream")

    return jsonify({
        "id": "chatcmpl-dummy",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "dummy",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    })


# Fixed vector returned for every input; dummy models have no real embeddings.
_EMBEDDING = [0.1, 0.2, 0.3, 0.4]


@app.post("/v1/embeddings")
def embeddings():
    inp = request.json.get("input", "")
    # A single string, a list of token ids, or a list of either: one vector per item.
    items = [inp] if isinstance(inp, str) or (inp and isinstance(inp[0], int)) else inp
    prompt_tokens = sum(len(i) // 4 if isinstance(i, str) else len(i) for i in items) or 1
    if request.json.get("encoding_format") == "base64":
        vector = base64.b64encode(struct.pack(f"<{len(_EMBEDDING)}f", *_EMBEDDING)).decode()
    else:
        vector = _EMBEDDING
    return jsonify({
        "object": "list",
        "model": "dummy",
        "data": [{"object": "embedding", "index": i, "embedding": vector} for i in range(len(items))],
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    })


def main():
    app.run(host="0.0.0.0", port=9999)


if __name__ == "__main__":
    main()
