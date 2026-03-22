"""Dummy OpenAI-compatible backend for load testing Lumen without hitting real models."""
import time
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.get("/v1/models")
def list_models():
    return jsonify({
        "object": "list",
        "data": [{"id": "dummy", "object": "model", "created": 0, "owned_by": "local"}],
    })


@app.post("/v1/chat/completions")
def chat_completions():
    return jsonify({
        "id": "chatcmpl-dummy",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "dummy",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    })


def main():
    app.run(host="0.0.0.0", port=9999)


if __name__ == "__main__":
    main()
