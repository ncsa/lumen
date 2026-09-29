"""Translation between the Anthropic Messages protocol and Lumen's OpenAI flow.

Pure functions plus one stream translator: no Flask, no DB, no config. The views
in ``blueprints/anthropic/routes.py`` own authentication, billing and the
upstream call (through the ``api`` blueprint's functions); everything here only
reshapes payloads.

Scope is the documented subset in ``docs/guides/anthropic.md``: text, image,
tool_use and tool_result content, tools, and the sampling parameters that have an
OpenAI equivalent. Anything outside it raises ``AnthropicError`` rather than
being dropped, except where dropping cannot change what the model is asked to do.
"""
import json
from http import HTTPStatus

from lumen.services.llm import estimate_prompt_tokens

# Versions accepted in the `anthropic-version` header. The header is optional
# here (the official clients always send it); an unknown value is refused rather
# than assumed, because a future version could change field meanings.
SUPPORTED_VERSIONS = frozenset({"2023-06-01", "2023-01-01"})

# Only beta capabilities exercised by the configured Claude Code client.
SUPPORTED_BETAS = frozenset({
    "claude-code-20250219", "interleaved-thinking-2025-05-14",
    "mid-conversation-system-2026-04-07", "effort-2025-11-24",
})
# Betas a client asks for often enough that the bare name is not a useful
# refusal, with what actually happens next. Matched on the feature name, so a
# client that bumps the date still gets the note.
_EXPLAINED_BETAS = {
    "structured-outputs-": (
        "Claude Code asks for this when it names a session and retries without it "
        "when refused, so the only cost is one extra round trip."
    ),
}
_UNSUPPORTED_FIELDS = ("mcp_servers", "container")
_DROPPED_BLOCKS = ("thinking", "redacted_thinking")


class AnthropicError(Exception):
    """A request Lumen refuses to translate: always a 400 invalid_request_error."""


# HTTP status for each Anthropic error type, so a failure answers with the status
# an Anthropic client expects for it.
ERROR_STATUS = {
    "invalid_request_error": HTTPStatus.BAD_REQUEST,
    "authentication_error": HTTPStatus.UNAUTHORIZED,
    "permission_error": HTTPStatus.FORBIDDEN,
    "not_found_error": HTTPStatus.NOT_FOUND,
    "rate_limit_error": HTTPStatus.TOO_MANY_REQUESTS,
    "api_error": HTTPStatus.INTERNAL_SERVER_ERROR,
    "overloaded_error": HTTPStatus.SERVICE_UNAVAILABLE,
}

# The error type to report for a status raised outside a view — routing, the
# limiter, an unhandled exception — where there is no Anthropic type to start
# from. Statuses Anthropic has no name for fall back to invalid_request_error.
STATUS_ERROR = {status: err_type for err_type, status in ERROR_STATUS.items()}

# The error types Lumen's OpenAI envelope uses, in Anthropic's vocabulary.
_FROM_OPENAI = {
    "authentication_error": "authentication_error",
    "insufficient_quota": "rate_limit_error",
    "rate_limit_error": "rate_limit_error",
    "server_error": "overloaded_error",
    "api_error": "api_error",
}


def error_body(message: str, err_type: str = "invalid_request_error") -> dict:
    """The Anthropic error envelope."""
    return {"type": "error", "error": {"type": err_type, "message": message}}


def error_type_for(openai_type: str, status: HTTPStatus) -> str:
    """Map shared errors, distinguishing access and lookup failures by status."""
    # Status first for the two Anthropic names that are a status: a 403 is a
    # permission_error whatever Lumen called it (a disabled account and a model
    # the caller may not use both arrive as "authentication_error").
    if status == HTTPStatus.FORBIDDEN:
        return "permission_error"
    if status == HTTPStatus.NOT_FOUND:
        return "not_found_error"
    if openai_type in _FROM_OPENAI:
        return _FROM_OPENAI[openai_type]
    return "api_error" if status >= HTTPStatus.INTERNAL_SERVER_ERROR else "invalid_request_error"


def check_headers(headers):
    """Validate the protocol headers. Raises AnthropicError."""
    version = headers.get("anthropic-version")
    if version and version not in SUPPORTED_VERSIONS:
        raise AnthropicError(
            f"Unsupported anthropic-version: {version}. Supported: "
            f"{', '.join(sorted(SUPPORTED_VERSIONS))}."
        )
    betas = {value.strip() for value in headers.get("anthropic-beta", "").split(",") if value.strip()}
    unsupported = sorted(betas - SUPPORTED_BETAS)
    if unsupported:
        message = f"Unsupported anthropic-beta: {', '.join(unsupported)}"
        notes = [note for prefix, note in _EXPLAINED_BETAS.items()
                 if any(beta.startswith(prefix) for beta in unsupported)]
        raise AnthropicError(". ".join([message, *notes]))


def _text(block):
    text = block.get("text")
    if not isinstance(text, str):
        raise AnthropicError("text blocks require a string text field")
    return text


def _system_text(system):
    """Flatten the top-level `system` field to one string."""
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(_text(block))
            else:
                raise AnthropicError("system blocks must be of type 'text'")
        return "\n\n".join(parts)
    raise AnthropicError("system must be a string or a list of text blocks")


def _image_url(block):
    """Anthropic image block -> OpenAI image_url part."""
    source = block.get("source")
    if not isinstance(source, dict):
        raise AnthropicError("image block requires a source object")
    kind = source.get("type")
    if kind == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not isinstance(data, str) or not media_type or not data:
            raise AnthropicError("base64 image source requires media_type and data")
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
    if kind == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise AnthropicError("url image source requires a url")
        return {"type": "image_url", "image_url": {"url": url}}
    raise AnthropicError(f"Unsupported image source type: {kind!r}")


def _tool_result_content(block):
    """Tool output as (text for the OpenAI tool message, image parts).

    OpenAI tool messages carry text only, so images in a tool result (Claude
    Code's Read tool returns one for a picture) are returned separately for the
    caller to send in the user message that follows the tool messages.
    """
    content = block.get("content")
    if content is None:
        content = ""
    images = []
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(_text(part))
            elif isinstance(part, dict) and part.get("type") == "image":
                images.append(_image_url(part))
            else:
                raise AnthropicError("tool_result supports only text and image content blocks")
        if images:
            texts.append(f"[{len(images)} image(s) from this tool result follow in the next message]")
        content = "\n".join(texts)
    if not isinstance(content, str):
        raise AnthropicError("tool_result content must be a string or a list of content blocks")
    if not isinstance(block.get("is_error", False), bool):
        raise AnthropicError("tool_result is_error must be a boolean")
    return (f"[Tool error] {content}" if block.get("is_error") else content), images


def _translate_user_message(content):
    """Translate a user turn, placing tool results before remaining user content."""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    tool_messages, tool_images, parts = [], [], []
    for block in content:
        if not isinstance(block, dict):
            raise AnthropicError("content blocks must be objects")
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": _text(block)})
        elif btype == "image":
            parts.append(_image_url(block))
        elif btype == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                raise AnthropicError("tool_result block requires tool_use_id")
            text, images = _tool_result_content(block)
            tool_messages.append({"role": "tool", "tool_call_id": tool_use_id, "content": text})
            tool_images.extend(images)
        elif btype in _DROPPED_BLOCKS:
            continue
        else:
            raise AnthropicError(f"Unsupported content block type: {btype!r}")

    messages = tool_messages
    # Tool-result images go first in the user message, right after the tool
    # messages they belong to; OpenAI requires those to stay contiguous.
    parts = tool_images + parts
    if parts:
        # A single text part collapses to a plain string: some backends reject
        # the structured form for text-only turns.
        if len(parts) == 1 and parts[0]["type"] == "text":
            messages = messages + [{"role": "user", "content": parts[0]["text"]}]
        else:
            messages = messages + [{"role": "user", "content": parts}]
    return messages


def _translate_assistant_message(content):
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]

    texts, tool_calls = [], []
    for block in content:
        if not isinstance(block, dict):
            raise AnthropicError("content blocks must be objects")
        btype = block.get("type")
        if btype == "text":
            texts.append(_text(block))
        elif btype == "tool_use":
            if (not isinstance(block.get("id"), str) or not block["id"]
                    or not isinstance(block.get("name"), str) or not block["name"]
                    or not isinstance(block.get("input"), dict)):
                raise AnthropicError("tool_use requires string id/name and an input object")
            tool_calls.append({
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block["input"]),
                },
            })
        elif btype in _DROPPED_BLOCKS:
            continue
        else:
            raise AnthropicError(f"Unsupported content block type: {btype!r}")

    if not texts and not tool_calls:
        # Nothing survived (an assistant turn of pure thinking blocks). A message
        # with neither content nor tool_calls is rejected by several backends, so
        # drop the turn; translate_request refuses a request left with none.
        return []
    message = {"role": "assistant", "content": "\n".join(texts) if texts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return [message]


def _translate_tools(tools):
    if not isinstance(tools, list):
        raise AnthropicError("tools must be a list")
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise AnthropicError("tools must be objects")
        # Server-side tools (web search, code execution, computer use) are named
        # by a versioned `type`; they run inside Anthropic's own service and have
        # no counterpart here.
        if tool.get("type") and tool.get("type") != "custom":
            raise AnthropicError(f"Unsupported tool type: {tool.get('type')!r}")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise AnthropicError("each tool requires a name")
        if not isinstance(tool.get("input_schema"), dict):
            raise AnthropicError("tool input_schema must be an object")
        function = {"name": name, "parameters": tool["input_schema"]}
        if tool.get("description"):
            function["description"] = tool["description"]
        out.append({"type": "function", "function": function})
    return out


# Anthropic's tool_choice types, in OpenAI's vocabulary. "tool" takes a name and
# is handled below.
_TOOL_CHOICE = {"auto": "auto", "any": "required", "none": "none"}


def _translate_tool_choice(choice):
    if not isinstance(choice, dict):
        raise AnthropicError("tool_choice must be an object")
    kind = choice.get("type")
    if not isinstance(kind, str):
        raise AnthropicError("tool_choice type must be a string")
    if kind in _TOOL_CHOICE:
        return _TOOL_CHOICE[kind]
    if kind == "tool":
        name = choice.get("name")
        if not isinstance(name, str) or not name:
            raise AnthropicError("tool_choice of type 'tool' requires a name")
        return {"type": "function", "function": {"name": name}}
    raise AnthropicError(f"Unsupported tool_choice type: {kind!r}")


def _append_user_text(messages, text):
    """Add text as user content without creating two user messages in a row.

    Some chat templates (Gemma's, older Mistral's) require strictly alternating
    roles. Appending to a trailing user message only changes the end of the
    conversation, so each request still extends the previous one.
    """
    last = messages[-1] if messages else None
    if last is None or last["role"] != "user":
        messages.append({"role": "user", "content": text})
    elif isinstance(last["content"], str):
        last["content"] = f"{last['content']}\n\n{text}"
    else:
        last["content"] = [*last["content"], {"type": "text", "text": text}]


def translate_request(data: dict, require_max_tokens: bool = True):
    """Translate to (model, messages, stream, kwargs, extra_body).

    Token counting does not require max_tokens. top_k travels in extra_body."""
    if not isinstance(data, dict):
        raise AnthropicError("Request body must be a JSON object")

    for field in _UNSUPPORTED_FIELDS:
        if data.get(field) is not None:
            raise AnthropicError(f"Unsupported field '{field}'")
    if data.get("thinking") not in (None, {"type": "disabled"}):
        raise AnthropicError("thinking controls are unsupported; disable thinking in the client")
    if data.get("context_management") not in (None, {}, {"edits": []}):
        raise AnthropicError("context_management edits are unsupported")
    if data.get("service_tier") not in (None, "auto"):
        raise AnthropicError("service_tier selection is unsupported")
    # output_config.effort is accepted and dropped. Anthropic's values are
    # low/medium/high/max; the chat templates behind Lumen each take their own
    # vocabulary (Qwen3.8's is low/medium/xhigh and its template raises on any
    # other value, which llama.cpp surfaces as a 500 for every request), so there
    # is no translation that is correct for more than one backend. Claude Code
    # sends effort on every request. Anything else in output_config (a JSON
    # schema under `format`) would change what the model returns and is refused.
    output = data.get("output_config")
    if output is None:
        output = {}
    if not isinstance(output, dict) or output.keys() - {"effort"}:
        raise AnthropicError("Only output_config.effort is supported")

    model = data.get("model")
    if not model or not isinstance(model, str):
        raise AnthropicError("model is required")

    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise AnthropicError("messages is required and must be a non-empty list")

    max_tokens = data.get("max_tokens")
    if require_max_tokens or max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            raise AnthropicError("max_tokens is required and must be a positive integer")

    stream = data.get("stream", False)
    if not isinstance(stream, bool):
        raise AnthropicError("stream must be a boolean")

    messages = []

    for message in raw_messages:
        if not isinstance(message, dict):
            raise AnthropicError("each message must be an object")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, (str, list)):
            raise AnthropicError("message content must be a string or a list of blocks")
        if role == "user":
            messages.extend(_translate_user_message(content))
        elif role == "assistant":
            messages.extend(_translate_assistant_message(content))
        elif role == "system":
            # Claude Code's mid-conversation-system beta puts system turns in the
            # messages array: its "# Environment" block after the first user turn,
            # and a token-budget line after every tool result. The chat templates
            # of the models Lumen fronts reject a system message anywhere but first
            # (Qwen3.8's raises, which llama.cpp and vLLM return as a 500), so the
            # turn is sent where it stands as a user message wrapped in
            # <system-reminder>, which is how Claude Code itself delivers such
            # context without the beta. Keeping it in place matters: moving it
            # into the leading system message changed that message on every turn,
            # so no request ever shared a prefix with the previous one and the
            # backend re-read the whole conversation each time.
            text = _system_text(content)
            if text:
                _append_user_text(messages, f"<system-reminder>\n{text}\n</system-reminder>")
        else:
            raise AnthropicError(f"Unsupported message role: {role!r}")

    system = _system_text(data.get("system"))
    if system:
        messages.insert(0, {"role": "system", "content": system})

    # A turn made entirely of blocks Lumen drops (thinking) leaves nothing to
    # send. Refusing here says what is wrong; forwarding an empty list makes the
    # backend answer with an error of its own that the caller cannot act on.
    if not messages:
        raise AnthropicError("messages contained nothing that could be sent to the model")

    kwargs = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    for field in ("temperature", "top_p"):
        if data.get(field) is not None:
            value = data[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise AnthropicError(f"{field} must be a number between 0 and 1")
            kwargs[field] = value
    if data.get("stop_sequences") is not None:
        stops = data["stop_sequences"]
        if not isinstance(stops, list) or any(not isinstance(stop, str) for stop in stops):
            raise AnthropicError("stop_sequences must be a list of strings")
        kwargs["stop"] = stops
    if data.get("tools") is not None:
        # An empty list is valid Anthropic and Claude Code sends one (its WebFetch
        # summarisation pass), but vLLM rejects `tools: []` with a 400, so the
        # field is dropped rather than forwarded empty. tool_choice goes with it:
        # it has nothing to choose from.
        tools = _translate_tools(data["tools"])
        if tools:
            kwargs["tools"] = tools
    if data.get("tool_choice") is not None:
        # Validated even when it will not be sent, so a malformed tool_choice is
        # still a 400 rather than something that quietly disappears with the
        # empty tools list above.
        choice = _translate_tool_choice(data["tool_choice"])
        # Anthropic hangs this off tool_choice; OpenAI has it as its own field.
        # Dropping it would let the model make several calls when the caller
        # asked for one.
        disable_parallel = data["tool_choice"].get("disable_parallel_tool_use", False)
        if not isinstance(disable_parallel, bool):
            raise AnthropicError("disable_parallel_tool_use must be a boolean")
        if kwargs.get("tools"):
            kwargs["tool_choice"] = choice
            if disable_parallel:
                kwargs["parallel_tool_calls"] = False

    # top_k has no OpenAI field; it rides in extra_body, where the backends that
    # support it read it and the ones that do not ignore it.
    extra_body = {}
    if data.get("top_k") is not None:
        top_k = data["top_k"]
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
            raise AnthropicError("top_k must be a non-negative integer")
        extra_body["top_k"] = top_k

    # `metadata` carries only a client-chosen user_id; Lumen bills the API key's
    # entity, so there is nothing to forward.
    return model, messages, stream, kwargs, extra_body


# ---------------------------------------------------------------------------
# Response translation
# ---------------------------------------------------------------------------

_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def stop_reason(finish_reason, has_tool_use=False, stop_sequence=None):
    """OpenAI finish_reason -> (stop_reason, stop_sequence).

    ``stop_sequence`` is the matched string when the backend reports one (vLLM
    puts it on the choice as ``stop_reason``); OpenAI itself does not distinguish
    a stop sequence from a natural end, so without that field the turn is
    reported as ``end_turn``.
    """
    if finish_reason == "stop" and isinstance(stop_sequence, str):
        return "stop_sequence", stop_sequence
    if finish_reason is None:
        return None, None
    reason = _STOP_REASONS.get(finish_reason, "end_turn")
    if reason == "end_turn" and has_tool_use:
        # Some backends report "stop" even when the turn ended in a tool call.
        reason = "tool_use"
    return reason, None


def _thinking_block(text):
    """A reasoning model's thinking phase, as an Anthropic thinking block.

    Reasoning backends put it in ``reasoning_content`` (or ``reasoning``) beside
    the answer, and it is billed as output tokens either way, so dropping it
    would charge for text the caller never sees. ``signature`` is Anthropic's
    proof that a block came from their model and there is nothing truthful to
    put there, so it is empty; a client passing the block back gets it dropped
    on the way in.
    """
    return {"type": "thinking", "thinking": text, "signature": ""}


def _tool_use_block(call):
    arguments = (call.get("function") or {}).get("arguments") or "{}"
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        # A backend that emits malformed arguments would otherwise 500 here.
        # Anthropic's `input` is an object, so the raw text is preserved under a
        # key rather than dropped.
        parsed = {"__raw_arguments": arguments}
    return {
        "type": "tool_use",
        "id": call.get("id") or "toolu_unknown",
        "name": (call.get("function") or {}).get("name") or "",
        "input": parsed,
    }


def _tool_input_json(fragments) -> str:
    """The streamed tool arguments as JSON the SDKs can parse.

    Malformed arguments get the same treatment as on the non-streaming path
    (``_tool_use_block``): the raw text under a key, rather than an invalid
    ``input_json_delta`` that makes the client SDK raise.
    """
    raw = "".join(fragments) or "{}"
    try:
        parsed = json.loads(raw)
    except ValueError:
        return json.dumps({"__raw_arguments": raw})
    return raw if isinstance(parsed, dict) else json.dumps({"__raw_arguments": raw})


def translate_response(payload: dict, model_name: str) -> dict:
    """OpenAI chat-completion dict -> Anthropic Message dict."""
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    content = []
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if reasoning:
        content.append(_thinking_block(reasoning))
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or ():
        content.append(_tool_use_block(call))

    usage = payload.get("usage") or {}
    reason, sequence = stop_reason(
        choice.get("finish_reason"),
        has_tool_use=bool(message.get("tool_calls")),
        stop_sequence=choice.get("stop_reason"),
    )
    return {
        "id": _message_id(payload.get("id")),
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content,
        "stop_reason": reason,
        "stop_sequence": sequence,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


def _message_id(upstream_id):
    """Anthropic clients expect a `msg_`-prefixed id; upstream ids are `chatcmpl-`."""
    if isinstance(upstream_id, str) and upstream_id.startswith("msg_"):
        return upstream_id
    return f"msg_{upstream_id}" if upstream_id else "msg_lumen"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def sse(event: str, data: dict = None) -> str:
    """One SSE event. Anthropic's payload always repeats the event name as its
    ``type``, so it is filled in here rather than at every call site."""
    return f"event: {event}\ndata: {json.dumps({'type': event, **(data or {})})}\n\n"


class StreamTranslator:
    """Context-free SSE translator. Buffer tools until finish() to serialize interleaved calls."""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._started = False
        self._message_id = "msg_lumen"
        self._block_index = -1
        # The type of the block currently open, or None between blocks.
        self._open = None
        self._tool_calls = []
        self._tool_by_index = {}
        self._finish_reason = None
        self._stop_sequence = None
        self._usage = {"input_tokens": 0, "output_tokens": 0}

    def chunk(self, payload: dict):
        """Yield the SSE events for one upstream chunk."""
        usage = payload.get("usage")
        if usage:
            self._usage = {"input_tokens": usage.get("prompt_tokens") or 0,
                           "output_tokens": usage.get("completion_tokens") or 0}

        if not self._started:
            self._message_id = _message_id(payload.get("id"))
            yield from self._start()

        choices = payload.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]
        if isinstance(choice.get("stop_reason"), str):
            self._stop_sequence = choice["stop_reason"]

        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            yield from self._open_block("thinking", _thinking_block(""))
            yield sse("content_block_delta", {
                "index": self._block_index,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            })

        text = delta.get("content")
        if text:
            yield from self._open_block("text", {"type": "text", "text": ""})
            yield sse("content_block_delta", {
                "index": self._block_index,
                "delta": {"type": "text_delta", "text": text},
            })

        for call in delta.get("tool_calls") or ():
            self._tool_call(call)

    def _start(self):
        self._started = True
        yield sse("message_start", {
            "message": {
                "id": self._message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model_name,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                # Real counts are only known from the terminal usage chunk; they
                # are reported in message_delta, which the SDKs merge into this
                # snapshot.
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def _open_block(self, kind: str, content_block: dict):
        """Close the previous block before opening a different content type."""
        if self._open != kind:
            yield from self._close_block()
            self._block_index += 1
            self._open = kind
            yield sse("content_block_start", {
                "index": self._block_index,
                "content_block": content_block,
            })

    def _tool_call(self, call):
        """Buffer interleaved arguments; one Anthropic block per complete call."""
        index = call.get("index", 0)
        call_id = call.get("id")
        function = call.get("function") or {}
        known = self._tool_by_index.get(index)
        if known is None or (call_id and call_id != known["id"]):
            known = {"id": call_id or f"toolu_{self._message_id}_{len(self._tool_calls)}",
                     "name": "", "arguments": []}
            self._tool_by_index[index] = known
            self._tool_calls.append(known)
        if function.get("name"):
            known["name"] += function["name"]
        if function.get("arguments"):
            known["arguments"].append(function["arguments"])

    def _close_block(self):
        if self._open is not None:
            if self._open == "thinking":
                yield sse("content_block_delta", {
                    "index": self._block_index,
                    "delta": {"type": "signature_delta", "signature": ""},
                })
            yield sse("content_block_stop", {
                "index": self._block_index,
            })
            self._open = None

    def finish(self):
        """Yield the terminal events. Called after a complete upstream stream."""
        if not self._started:
            yield from self._start()
        yield from self._close_block()
        for call in self._tool_calls:
            yield from self._open_block("tool_use", {
                "type": "tool_use", "id": call["id"], "name": call["name"], "input": {},
            })
            yield sse("content_block_delta", {
                "index": self._block_index,
                "delta": {"type": "input_json_delta", "partial_json": _tool_input_json(call["arguments"])},
            })
            yield from self._close_block()
        reason, sequence = stop_reason(
            self._finish_reason or "stop",
            has_tool_use=bool(self._tool_calls),
            stop_sequence=self._stop_sequence,
        )
        yield sse("message_delta", {
            "delta": {"stop_reason": reason, "stop_sequence": sequence},
            "usage": self._usage,
        })
        yield sse("message_stop")

    def error(self, message: str, err_type: str = "api_error"):
        """Render a shared streaming failure in the Anthropic envelope.

        The OpenAI stream carries only the error's type, not its status. It sends
        "api_error"/"server_error" for failures on Lumen's or the backend's side
        and the backend's own type for a 4xx (a context-length error, say), which
        must stay a caller error so the client does not retry it.
        """
        status = (HTTPStatus.INTERNAL_SERVER_ERROR if err_type in ("api_error", "server_error")
                  else HTTPStatus.BAD_REQUEST)
        yield sse("error", error_body(message, error_type_for(err_type, status)))


def count_tokens(messages, tools=None) -> int:
    """Estimate prompt tokens, including tool definitions and prior tool arguments. Never billed."""
    extra = [json.dumps(tools)] if tools else []
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or ():
            extra.append(json.dumps(call.get("function") or {}))
    # Counted through the same estimator rather than re-deriving its ratio here.
    return estimate_prompt_tokens(messages) + estimate_prompt_tokens(
        [{"role": "user", "content": "".join(extra)}])
