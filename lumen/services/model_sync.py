"""
Fetch model metadata from vLLM/SGLang endpoints and models.dev.
Mirrors the logic in the top-level sync_models.py script, but designed
for use from the web process.  models.dev is cached in-process for TTL
seconds so repeated calls during a config-editor session only hit the
remote API once.
"""

import re
import time

import requests

MODELSDEV_URL = "https://models.dev/api.json"
ENDPOINT_TIMEOUT = 10
MODELSDEV_TIMEOUT = 15
OBSOLETE_FIELDS = ["supports_vision"]

_TTL = 600  # 10 minutes

_cache: dict = {"data": None, "ts": 0.0, "index": {}}


# ---------------------------------------------------------------------------
# models.dev fetch + cache
# ---------------------------------------------------------------------------

def get_modelsdev() -> tuple[list[dict], dict[str, list[dict]]]:
    """Return a (models, price_index) snapshot from the TTL cache.

    Both come from the same fetch so callers can't see a torn view if a
    concurrent request refreshes the cache between two separate reads.
    """
    now = time.monotonic()
    if _cache["data"] is None or now - _cache["ts"] > _TTL:
        _cache["data"] = _fetch_modelsdev()
        _cache["ts"] = now
        _cache["index"] = _build_price_index(_cache["data"])
    return _cache["data"] or [], _cache["index"]


def _fetch_modelsdev() -> list[dict]:
    try:
        r = requests.get(MODELSDEV_URL, timeout=MODELSDEV_TIMEOUT)
        result = []
        for provider in r.json().values():
            if isinstance(provider, dict):
                for model in provider.get("models", {}).values():
                    if isinstance(model, dict):
                        result.append(model)
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Endpoint probe
# ---------------------------------------------------------------------------

def fetch_endpoint_model(endpoint: dict) -> dict | None:
    base = endpoint["url"].rstrip("/")
    headers = {"Authorization": f"Bearer {endpoint.get('api_key', '')}"}

    # SGLang: /get_server_info is the authoritative source — it exposes
    # max_req_input_len (the real per-request limit from --context-length,
    # not the model's theoretical max), is_embedding, and enable_multimodal.
    # A 200 means we hit SGLang. We capture the capability flags even when
    # max_req_input_len is absent (e.g. embedding-only deployments) so they
    # survive the fallback chain below.
    sglang_flags: dict = {}
    try:
        r = requests.get(f"{base}/get_server_info", headers=headers, timeout=ENDPOINT_TIMEOUT)
        if r.ok:
            info = r.json()
            sglang_flags = {
                "backend": "sglang",
                "is_embedding": bool(info.get("is_embedding")),
                "enable_multimodal": info.get("enable_multimodal"),
            }
            mrl = info.get("max_req_input_len")
            if mrl is not None:
                return {"id": info.get("served_model_name") or "", "max_model_len": mrl, **sglang_flags}
    except Exception:
        pass

    # vLLM (or SGLang without max_req_input_len): /v1/models gives id + max_model_len.
    model_id = None
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=ENDPOINT_TIMEOUT)
        models = r.json().get("data", [])
        if models:
            model_id = models[0].get("id")
            mml = models[0].get("max_model_len")
            if mml is not None:
                return {"id": model_id or "", "max_model_len": mml, **sglang_flags}
    except Exception:
        pass

    # Older SGLang: /get_model_info returns context_length.
    try:
        r = requests.get(f"{base}/get_model_info", headers=headers, timeout=ENDPOINT_TIMEOUT)
        info = r.json()
        if "context_length" in info:
            return {"id": model_id or info.get("model_path", ""), "max_model_len": info["context_length"], **sglang_flags}
    except Exception:
        pass

    # SGLang responded with capability flags but no context length anywhere —
    # still return them so the modality override can fire.
    if sglang_flags:
        return {"id": model_id or "", "max_model_len": None, **sglang_flags}

    return None


# ---------------------------------------------------------------------------
# models.dev fuzzy matching (same algorithm as sync_models.py)
# ---------------------------------------------------------------------------

_NOISE = re.compile(
    r"^\d{2,}[bm]?$"
    r"|^fp\d+$|^bf\d+$|^q\d.*$|^[abkm]$"
    r"|^instruct$|^chat$|^hf$|^gguf$|^it$|^preview$"
)


def _tokens(s: str) -> set[str]:
    s = s.split(":")[0]
    s = re.sub(r"[-_](?:fp|bf)\d+$", "", s, flags=re.IGNORECASE)
    parts = re.split(r"[-_/.]+", s.lower())
    expanded: list[str] = []
    for p in parts:
        expanded.extend(re.split(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", p))
    return {t for t in expanded if t and not _NOISE.match(t)}


def _normalize_id(s: str) -> str:
    s = s.split("/")[-1].split(":")[0]
    return re.sub(r"[-_.]", "", s).lower()


# Distinctive task/modality tokens. A model carrying one of these must not be
# fuzzy-matched to a candidate that lacks it (e.g. a speech model to a text one).
_KIND_TOKENS = {
    "speech", "audio", "voice", "tts", "stt", "asr", "whisper",
    "vision", "embed", "embedding", "rerank", "reranker", "ocr",
    "moderation", "guard",
}


def find_in_modelsdev(model_id: str, models: list[dict], config_name: str | None = None) -> dict | None:
    if config_name:
        needle = _normalize_id(config_name)
        for m in models:
            if _normalize_id(m.get("id", "")) == needle:
                return m

    base = model_id.split("/")[-1] if "/" in model_id else model_id
    needle = _tokens(base)
    if not needle:
        return None
    needle_versions = {t for t in needle if t.isdigit()}
    needle_kind = needle & _KIND_TOKENS

    best, best_score = None, 0
    for m in models:
        hay = _tokens(m.get("id", "") + " " + m.get("name", ""))
        hay_versions = {t for t in hay if t.isdigit()}
        if needle_versions and hay_versions and not (needle_versions & hay_versions):
            continue
        # A distinctive task/modality token must agree (don't match speech↔text, etc.).
        hay_kind = hay & _KIND_TOKENS
        if (needle_kind or hay_kind) and not (needle_kind & hay_kind):
            continue
        score = len(needle & hay)
        if score > best_score and score >= 2:
            best, best_score = m, score
    return best


# ---------------------------------------------------------------------------
# Pricing (average across providers)
# ---------------------------------------------------------------------------

def _build_price_index(models: list[dict]) -> dict[str, list[dict]]:
    """Group models.dev entries by normalized id, once, so _average_price is
    an O(1) lookup instead of re-scanning the whole catalogue per model."""
    idx: dict[str, list[dict]] = {}
    for m in models:
        nid = _normalize_id(m.get("id", ""))
        if nid:
            idx.setdefault(nid, []).append(m)
    return idx


def _average_price(dev_match: dict, price_index: dict[str, list[dict]]) -> tuple[float | None, float | None]:
    """Average input/output cost (USD per million tokens) across every
    models.dev provider that lists the same base model, ignoring zero/missing
    prices (a $0 listing is not a real price and would skew the average down).

    Returns (input_avg, output_avg); each component is None when no provider
    exposes a usable (>0) price for it.
    """
    needle = _normalize_id(dev_match.get("id", ""))
    if not needle:
        return None, None
    inputs: list[float] = []
    outputs: list[float] = []
    for m in price_index.get(needle, []):
        c = m.get("cost") or {}
        try:
            ci = float(c.get("input") or 0)
            co = float(c.get("output") or 0)
        except (TypeError, ValueError):
            continue
        if ci > 0:
            inputs.append(ci)
        if co > 0:
            outputs.append(co)
    avg_in = round(sum(inputs) / len(inputs), 6) if inputs else None
    avg_out = round(sum(outputs) / len(outputs), 6) if outputs else None
    return avg_in, avg_out


# ---------------------------------------------------------------------------
# Server-authoritative modalities
# ---------------------------------------------------------------------------

def _server_modalities(ep_model: dict, pending_in, current_in) -> tuple:
    """Return (srv_in, srv_out) from SGLang server capability flags.

    Each component is the authoritative modality list, or None to mean
    "don't touch this field". pending_in is the input_modalities already
    proposed by models.dev (if any); current_in is the operator's current
    value — used as the base to which image is added when multimodal is on.
    """
    if ep_model.get("is_embedding"):
        return ["text"], []
    if ep_model.get("enable_multimodal") is True:
        base = pending_in or current_in or ["text"]
        return (base if "image" in base else [*base, "image"]), None
    return ["text"], None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def sync_model(model_def: dict) -> dict:
    """Return proposed updates for one model definition.

    Returns a dict with:
      updates   – {field: new_value} for fields that changed
      removals  – list of obsolete field names present in model_def
      matched   – models.dev model id that was matched, or None
      endpoint_ok – whether at least one endpoint responded
    """
    dev_models, price_index = get_modelsdev()

    ep_model = None
    for ep in model_def.get("endpoints", []):
        ep_model = fetch_endpoint_model(ep)
        if ep_model:
            break

    dev_match = None
    if dev_models:
        ep_id = (model_def.get("endpoints") or [{}])[0].get("model") or model_def.get("name", "")
        dev_match = find_in_modelsdev(ep_id, dev_models, config_name=model_def.get("name"))

    updates: dict = {}
    dev_limit = (dev_match.get("limit") or {}) if dev_match else {}

    # context_window is the total input+output budget. The endpoint's
    # max_model_len is authoritative — an operator can configure it below the
    # model's theoretical max — so prefer it and fall back to models.dev.
    ep_ctx = ep_model.get("max_model_len") if ep_model else None
    context_window = ep_ctx if ep_ctx is not None else dev_limit.get("context")
    if context_window is not None and model_def.get("context_window") != context_window:
        updates["context_window"] = context_window

    # max_output_tokens is a separate cap on generation only. A vLLM/SGLang
    # endpoint shares one budget and has no such cap, so only models.dev can
    # supply it; leave the field untouched when there is no match.
    out_tokens = dev_limit.get("output")
    if out_tokens is not None and model_def.get("max_output_tokens") != out_tokens:
        updates["max_output_tokens"] = out_tokens

    # A models.dev match is trusted to correct these fields (the matcher guards against
    # cross-version and cross-kind mis-matches, so a match means the same model). When there
    # is no match, nothing here is touched, so operator-set values are preserved.
    if dev_match:
        for field, new_val in [
            ("knowledge_cutoff",   dev_match.get("knowledge")),
            ("supports_reasoning", dev_match.get("reasoning")),
            ("input_modalities",   (dev_match.get("modalities") or {}).get("input")),
            ("output_modalities",  (dev_match.get("modalities") or {}).get("output")),
        ]:
            if new_val is not None and model_def.get(field) != new_val:
                updates[field] = new_val

        # Pricing: average across providers offering the same base model on
        # models.dev, excluding $0 listings. Overwrites a stale/zero operator
        # value the same way other fields are corrected on a trusted match.
        avg_in, avg_out = _average_price(dev_match, price_index)
        if avg_in is not None and model_def.get("input_cost_per_million") != avg_in:
            updates["input_cost_per_million"] = avg_in
        if avg_out is not None and model_def.get("output_cost_per_million") != avg_out:
            updates["output_cost_per_million"] = avg_out

    # SGLang /get_server_info capability flags are authoritative over models.dev
    # — they reflect what the operator actually enabled on the serving backend,
    # so a disabled modality on the server vetoes a models.dev claim.
    if ep_model and ep_model.get("backend") == "sglang":
        srv_in, srv_out = _server_modalities(
            ep_model,
            updates.get("input_modalities"),
            model_def.get("input_modalities"),
        )
        if srv_in is not None:
            if model_def.get("input_modalities") != srv_in:
                updates["input_modalities"] = srv_in
            elif "input_modalities" in updates:
                del updates["input_modalities"]
        if srv_out is not None:
            if model_def.get("output_modalities") != srv_out:
                updates["output_modalities"] = srv_out
            elif "output_modalities" in updates:
                del updates["output_modalities"]

    removals = [f for f in OBSOLETE_FIELDS if f in model_def]

    return {
        "updates": updates,
        "removals": removals,
        "matched": dev_match.get("id") if dev_match else None,
        "endpoint_ok": ep_model is not None,
    }
