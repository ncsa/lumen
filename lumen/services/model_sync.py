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

    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=ENDPOINT_TIMEOUT)
        models = r.json().get("data", [])
        if models and models[0].get("max_model_len") is not None:
            return models[0]
        model_id = models[0].get("id") if models else None
    except Exception:
        model_id = None

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

    best, best_score = None, 0
    for m in models:
        hay = _tokens(m.get("id", "") + " " + m.get("name", ""))
        hay_versions = {t for t in hay if t.isdigit()}
        if needle_versions and hay_versions and not (needle_versions & hay_versions):
            continue
        score = len(needle & hay)
        if score > best_score and score >= 2:
            best, best_score = m, score
    return best


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

    if dev_match:
        for field, new_val in [
            ("knowledge_cutoff",   dev_match.get("knowledge")),
            ("supports_reasoning", dev_match.get("reasoning")),
            ("input_modalities",   (dev_match.get("modalities") or {}).get("input")),
            ("output_modalities",  (dev_match.get("modalities") or {}).get("output")),
        ]:
            if new_val is not None and model_def.get(field) != new_val:
                updates[field] = new_val

    removals = [f for f in OBSOLETE_FIELDS if f in model_def]

    return {
        "updates": updates,
        "removals": removals,
        "matched": dev_match.get("id") if dev_match else None,
        "endpoint_ok": ep_model is not None,
    }
