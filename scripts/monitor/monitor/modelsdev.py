"""models.dev provider prices for a Lumen model.

The fetch and the fuzzy matcher are copied from `lumen/services/model_sync.py`
(`_fetch_modelsdev`, `_tokens`, `_normalize_id`, `find_in_modelsdev`,
`_build_price_index`); keep them in step (see AGENTS.md). Unlike Lumen, which
averages the provider prices, the monitor records their median, p5 and p95.
"""

import re
import statistics

import requests

MODELSDEV_URL = "https://models.dev/api.json"


def fetch_modelsdev(timeout: float) -> list[dict]:
    """Every model listing of every provider; empty when models.dev cannot be read."""
    try:
        r = requests.get(MODELSDEV_URL, timeout=timeout)
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
# Matcher (copy of lumen/services/model_sync.py)
# ---------------------------------------------------------------------------

_NOISE = re.compile(
    r"^\d{2,}[bm]?$"
    r"|^\d$"                       # single digits are version fragments, not distinctive
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

# A number that is a model size (70b, a3b, 125m, 8x7b), a precision (fp8, bf16, int4) or a
# quantization (q5_k_m) is not a version.
_NOT_VERSION_BEFORE = re.compile(r"(?:fp|bf|int|q)$")
_NOT_VERSION_AFTER = re.compile(r"[bmk](?![a-z])|x\d")


def _versions(s: str) -> set[str]:
    """The major version numbers in an id: "2" in coder-v2, "4" in v4.1, "3" in qwen3.

    _tokens drops digits so they never count towards a match; the version guard reads
    the generations from here instead."""
    s = s.split(":")[0].lower()
    found = set()
    for m in re.finditer(r"(\d+)(?:\.\d+)*", s):
        if _NOT_VERSION_BEFORE.search(s, 0, m.start()) or _NOT_VERSION_AFTER.match(s, m.end()):
            continue
        found.add(m.group(1).lstrip("0") or "0")
    return found


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
    needle_versions = _versions(base)
    needle_kind = needle & _KIND_TOKENS

    best, best_score = None, 0
    for m in models:
        hay = _tokens(m.get("id", "") + " " + m.get("name", ""))
        hay_versions = _versions(m.get("id", "") + " " + m.get("name", ""))
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


def build_price_index(models: list[dict]) -> dict[str, list[dict]]:
    """Group models.dev entries by normalized id, so every provider listing of a match is one lookup."""
    idx: dict[str, list[dict]] = {}
    for m in models:
        nid = _normalize_id(m.get("id", ""))
        if nid:
            idx.setdefault(nid, []).append(m)
    return idx


# ---------------------------------------------------------------------------
# Monitor additions: pinning and the price range
# ---------------------------------------------------------------------------

def match(model_id: str, root: str | None, models: list[dict], pin: str | None = None) -> dict | None:
    """The models.dev listing for a Lumen model.

    With a `modelsdev_id` pin only that id matches (no fuzzy fallback). Otherwise
    match as Lumen does: the Lumen id exactly, then the upstream `root` fuzzily.
    """
    if pin:
        needle = _normalize_id(pin)
        return next((m for m in models if _normalize_id(m.get("id", "")) == needle), None)
    return find_in_modelsdev(root or model_id, models, config_name=model_id)


def price_range(values: list[float]) -> tuple[float, float, float] | None:
    """(median, p5, p95) of `values`; None when empty. One value gives p5 = p95 = median."""
    if not values:
        return None
    median = statistics.median(values)
    if len(values) == 1:
        return median, median, median
    cuts = statistics.quantiles(values, n=20, method="inclusive")
    return median, cuts[0], cuts[-1]


def _price(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def provider_stats(dev_match: dict, price_index: dict[str, list[dict]]) -> dict | None:
    """Median, p5 and p95 of the non-zero input and output prices ($/M tokens) of
    every provider listing the matched model, plus how many listings had a price.
    None when no provider lists a price above $0."""
    inputs: list[float] = []
    outputs: list[float] = []
    providers = 0
    for m in price_index.get(_normalize_id(dev_match.get("id", "")), []):
        cost = m.get("cost") or {}
        ci, co = _price(cost.get("input")), _price(cost.get("output"))
        if ci > 0:
            inputs.append(ci)
        if co > 0:
            outputs.append(co)
        if ci > 0 or co > 0:
            providers += 1
    if not providers:
        return None
    stats = {"modelsdev_id": dev_match.get("id"), "providers": providers}
    for kind, values in (("input", inputs), ("output", outputs)):
        median, p5, p95 = price_range(values) or (None, None, None)
        stats.update({f"{kind}_median": median, f"{kind}_p5": p5, f"{kind}_p95": p95})
    return stats
