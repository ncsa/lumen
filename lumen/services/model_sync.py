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

_cache: dict = {"data": None, "ts": 0.0}


# ---------------------------------------------------------------------------
# models.dev fetch + cache
# ---------------------------------------------------------------------------

def get_modelsdev() -> list[dict]:
    now = time.monotonic()
    if _cache["data"] is None or now - _cache["ts"] > _TTL:
        _cache["data"] = _fetch_modelsdev()
        _cache["ts"] = now
    return _cache["data"] or []


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
    # A 200 means we hit SGLang and have everything; skip /v1/models entirely.
    try:
        r = requests.get(f"{base}/get_server_info", headers=headers, timeout=ENDPOINT_TIMEOUT)
        if r.ok:
            info = r.json()
            mrl = info.get("max_req_input_len")
            if mrl is not None:
                return {
                    "id": info.get("served_model_name") or "",
                    "max_model_len": mrl,
                    "is_embedding": bool(info.get("is_embedding")),
                    "enable_multimodal": info.get("enable_multimodal"),
                }
    except Exception:
        pass

    # vLLM (or SGLang without /get_server_info): /v1/models gives id + max_model_len.
    model_id = None
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=ENDPOINT_TIMEOUT)
        models = r.json().get("data", [])
        if models:
            model_id = models[0].get("id")
            mml = models[0].get("max_model_len")
            if mml is not None:
                return {"id": model_id or "", "max_model_len": mml}
    except Exception:
        pass

    # Older SGLang: /get_model_info returns context_length.
    try:
        r = requests.get(f"{base}/get_model_info", headers=headers, timeout=ENDPOINT_TIMEOUT)
        info = r.json()
        if "context_length" in info:
            return {"id": model_id or info.get("model_path", ""), "max_model_len": info["context_length"]}
    except Exception:
        pass

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

def _average_price(dev_match: dict, dev_models: list[dict]) -> tuple[float | None, float | None]:
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
    for m in dev_models:
        if _normalize_id(m.get("id", "")) != needle:
            continue
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
    dev_models = get_modelsdev()

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
        avg_in, avg_out = _average_price(dev_match, dev_models)
        if avg_in is not None and model_def.get("input_cost_per_million") != avg_in:
            updates["input_cost_per_million"] = avg_in
        if avg_out is not None and model_def.get("output_cost_per_million") != avg_out:
            updates["output_cost_per_million"] = avg_out

    # SGLang /get_server_info capability flags are authoritative over models.dev
    # — they reflect what the operator actually enabled on the serving backend,
    # so a disabled modality on the server vetoes a models.dev claim. (Only
    # present when the endpoint was reached via /get_server_info.)
    if ep_model and "enable_multimodal" in ep_model:
        if ep_model.get("is_embedding"):
            srv_in, srv_out = ["text"], []
        elif ep_model.get("enable_multimodal") is True:
            base = updates.get("input_modalities") or model_def.get("input_modalities") or ["text"]
            srv_in = base if "image" in base else [*base, "image"]
            srv_out = None
        else:
            srv_in, srv_out = ["text"], None

        for field, srv in (("input_modalities", srv_in), ("output_modalities", srv_out)):
            if srv is None:
                continue
            if model_def.get(field) != srv:
                updates[field] = srv
            elif field in updates:
                del updates[field]

    removals = [f for f in OBSOLETE_FIELDS if f in model_def]

    return {
        "updates": updates,
        "removals": removals,
        "matched": dev_match.get("id") if dev_match else None,
        "endpoint_ok": ep_model is not None,
    }
