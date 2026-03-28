"""Dummy OpenAI-compatible backend for load testing Lumen without hitting real models."""
import json
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


def main():
    app.run(host="0.0.0.0", port=9999)


if __name__ == "__main__":
    main()
