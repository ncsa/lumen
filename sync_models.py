#!/usr/bin/env python3
"""
Sync model metadata from vLLM endpoints and models.dev into lumen.config.yaml.

Usage:
    uv run python sync_models.py [path/to/lumen.config.yaml]

Checks each model against:
  - Its vLLM or SGLang endpoint  → context_window, max_output_tokens
  - models.dev                   → knowledge_cutoff, supports_reasoning, input_modalities, output_modalities

Shows proposed changes one model at a time and prompts before writing.
Inline comments in the config file are preserved.
Missing fields are inserted before `endpoints:`. Obsolete fields are removed.
"""

import json
import re
import sys
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"
MODELSDEV_URL = "https://models.dev/api.json"
TIMEOUT = 10

# Fields the script manages
CAPABILITY_FIELDS = [
    "context_window",
    "max_output_tokens",
    "supports_reasoning",
    "knowledge_cutoff",
    "input_modalities",
    "output_modalities",
]

# Fields that have been superseded and should be removed if present
OBSOLETE_FIELDS = ["supports_vision"]


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_endpoint_model(endpoint: dict) -> dict | None:
    base = endpoint["url"].rstrip("/")
    headers = {"Authorization": f"Bearer {endpoint['api_key']}"}

    # vLLM: /v1/models returns max_model_len directly
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=TIMEOUT)
        models = r.json().get("data", [])
        if models and models[0].get("max_model_len") is not None:
            return models[0]
        # SGLang: /v1/models exists but lacks max_model_len — fall through
        model_id = models[0].get("id") if models else None
    except Exception:
        model_id = None

    # SGLang: /get_model_info returns context_length
    try:
        r = requests.get(f"{base}/get_model_info", headers=headers, timeout=TIMEOUT)
        info = r.json()
        if "context_length" in info:
            return {
                "id": model_id or info.get("model_path", ""),
                "max_model_len": info["context_length"],
            }
    except Exception:
        pass

    return None


def fetch_modelsdev() -> list[dict]:
    # Structure: {provider_id: {"id": ..., "models": {model_id: model_obj}}}
    try:
        r = requests.get(MODELSDEV_URL, timeout=15)
        result = []
        for provider in r.json().values():
            if isinstance(provider, dict):
                for model in provider.get("models", {}).values():
                    if isinstance(model, dict):
                        result.append(model)
        return result
    except Exception as e:
        print(f"  Warning: could not fetch models.dev: {e}")
    return []


# ---------------------------------------------------------------------------
# models.dev fuzzy matching
# ---------------------------------------------------------------------------

_NOISE = re.compile(
    r"^\d{2,}[bm]?$"    # parameter sizes: 27b, 30b, 120b (keep single digits for version matching)
    r"|^fp\d+$"          # precisions: fp8, fp16
    r"|^bf\d+$"          # bf16
    r"|^q\d.*$"          # quantization: q5_k_m
    r"|^[abkm]$"         # lone letters left after splitting size tokens (e.g. "27b" → "27"+"b")
    r"|^instruct$|^chat$|^hf$|^gguf$|^it$|^preview$"
)


def _tokens(s: str) -> set[str]:
    s = s.split(":")[0]                                            # strip variant suffix
    s = re.sub(r"[-_](?:fp|bf)\d+$", "", s, flags=re.IGNORECASE)  # strip trailing -fp8, -bf16
    parts = re.split(r"[-_/.]+", s.lower())
    # Split letter→digit and digit→letter so "gemma4" → "gemma","4"
    expanded = []
    for p in parts:
        expanded.extend(re.split(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", p))
    return {t for t in expanded if t and not _NOISE.match(t)}


def _normalize_id(s: str) -> str:
    """Normalize for exact comparison: drop provider prefix, variant suffix, and separators."""
    s = s.split("/")[-1]
    s = s.split(":")[0]
    s = re.sub(r"[-_.]", "", s)
    return s.lower()


def find_in_modelsdev(model_id: str, models: list[dict], config_name: str | None = None) -> dict | None:
    # 1. Exact match on normalized config name (handles "gemma4-31b" ↔ "gemma-4-31b")
    if config_name:
        needle = _normalize_id(config_name)
        for m in models:
            if _normalize_id(m.get("id", "")) == needle:
                return m

    # 2. Fuzzy match with version conflict guard
    base = model_id.split("/")[-1] if "/" in model_id else model_id
    needle = _tokens(base)
    if not needle:
        return None

    # Single-digit tokens are generation numbers — both sides must agree if present
    needle_versions = {t for t in needle if t.isdigit()}

    best, best_score = None, 0
    for m in models:
        hay = _tokens(m.get("id", "") + " " + m.get("name", ""))
        hay_versions = {t for t in hay if t.isdigit()}
        if needle_versions and hay_versions and not (needle_versions & hay_versions):
            continue  # generation mismatch (e.g. gemma-4 vs gemma-3)
        score = len(needle & hay)
        if score > best_score and score >= 2:
            best, best_score = m, score
    return best


# ---------------------------------------------------------------------------
# Change computation
# ---------------------------------------------------------------------------

def compute_changes(
    model_def: dict, ep_model: dict | None, dev_model: dict | None
) -> tuple[dict, list[str]]:
    """Return (changes, removals) where changes maps field → (old, new) and
    removals is a list of obsolete field names present in model_def."""
    changes = {}

    if ep_model:
        max_len = ep_model.get("max_model_len")
        if max_len is not None:
            for field in ("context_window", "max_output_tokens"):
                if model_def.get(field) != max_len:
                    changes[field] = (model_def.get(field), max_len)

    if dev_model:
        for field, new_val in [
            ("knowledge_cutoff",   dev_model.get("knowledge")),
            ("supports_reasoning", dev_model.get("reasoning")),
            ("input_modalities",   (dev_model.get("modalities") or {}).get("input")),
            ("output_modalities",  (dev_model.get("modalities") or {}).get("output")),
        ]:
            if new_val is not None and model_def.get(field) != new_val:
                changes[field] = (model_def.get(field), new_val)

    removals = [f for f in OBSOLETE_FIELDS if f in model_def]
    return changes, removals


# ---------------------------------------------------------------------------
# Config text patching (preserves inline comments)
# ---------------------------------------------------------------------------

def _yaml_inline(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        inner = ", ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in value)
        return f"[{inner}]"
    return str(value)


def _field_indent(section: str, fallback: str) -> str:
    m = re.search(r'^(\s+)\w+:', section, re.MULTILINE)
    return m.group(1) if m else fallback + "  "


def patch_config_text(
    text: str, model_name: str, changes: dict, removals: list[str]
) -> str:
    start_m = re.search(
        rf'^(\s+)-\s+name:\s+{re.escape(model_name)}\s*$', text, re.MULTILINE
    )
    if not start_m:
        print(f"  Warning: could not locate '{model_name}' in config text")
        return text

    indent = start_m.group(1)
    block_start = start_m.start()
    next_m = re.search(
        rf'^{re.escape(indent)}-\s+name:\s+', text[start_m.end():], re.MULTILINE
    )
    block_end = start_m.end() + next_m.start() if next_m else len(text)
    section = text[block_start:block_end]

    fld_indent = _field_indent(section, indent)

    # Remove obsolete fields
    for field in removals:
        section = re.sub(
            rf'^{re.escape(fld_indent)}{re.escape(field)}:.*\n',
            "", section, flags=re.MULTILINE,
        )

    # Update or insert capability fields
    for field, (_, new_val) in changes.items():
        val_str = _yaml_inline(new_val)
        patched = re.sub(
            rf'^(\s+{re.escape(field)}:\s*)[^\n#]*((?:\s+#[^\n]*)?)\s*$',
            lambda m, v=val_str: m.group(1) + v + m.group(2),
            section, flags=re.MULTILINE,
        )
        if patched == section:
            # Field missing — insert before endpoints:
            insert_line = f"{fld_indent}{field}: {val_str}\n"
            ep_m = re.search(
                rf'^{re.escape(fld_indent)}endpoints:', section, re.MULTILINE
            )
            if ep_m:
                section = section[:ep_m.start()] + insert_line + section[ep_m.start():]
            else:
                section = section.rstrip("\n") + "\n" + insert_line
        else:
            section = patched

    return text[:block_start] + section + text[block_end:]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fmt(v) -> str:
    if isinstance(v, list):
        return json.dumps(v)
    return str(v) if v is not None else "null"


def main():
    args = sys.argv[1:]
    auto_yes = "--yes" in args or "-y" in args
    args = [a for a in args if a not in ("--yes", "-y")]
    config_path = Path(args[0]) if args else CONFIG_PATH
    config_text = config_path.read_text()
    config = yaml.safe_load(config_text)

    print("Fetching models.dev ... ", end="", flush=True)
    dev_models = fetch_modelsdev()
    print(f"{len(dev_models)} models\n" if dev_models else "failed\n")

    any_changes = False

    for model_def in config.get("models", []):
        name = model_def["name"]
        if not model_def.get("active", True):
            print(f"  {name}: skipped (inactive)")
            continue
        print(f"{'─' * 54}")
        print(f"  {name}")
        print(f"{'─' * 54}")

        ep_model = None
        for ep in model_def.get("endpoints", []):
            print(f"  {ep['url']} ... ", end="", flush=True)
            ep_model = fetch_endpoint_model(ep)
            if ep_model:
                print(f"ok (max_model_len={ep_model.get('max_model_len')})")
                break
            else:
                print("unreachable")

        dev_model = None
        if dev_models:
            ep_id = (model_def.get("endpoints") or [{}])[0].get("model") or name
            dev_model = find_in_modelsdev(ep_id, dev_models, config_name=name)
            print(
                f"  models.dev: {dev_model.get('id', '?')}"
                if dev_model else "  models.dev: no match"
            )

        changes, removals = compute_changes(model_def, ep_model, dev_model)

        if not changes and not removals:
            print("  No changes.\n")
            continue

        print("\n  Proposed changes:")
        for field in removals:
            print(f"    - remove {field}: {fmt(model_def[field])}")
        for field, (old, new) in changes.items():
            label = "add" if field not in model_def else "update"
            print(f"    {label} {field}:  {fmt(old)}  →  {fmt(new)}")

        if auto_yes:
            ans = "y"
        else:
            ans = input("\n  Apply? [y/N] ").strip().lower()
        if ans == "y":
            config_text = patch_config_text(config_text, name, changes, removals)
            any_changes = True
            print("  Applied.\n")
        else:
            print("  Skipped.\n")

    if any_changes:
        config_path.write_text(config_text)
        print(f"Saved {config_path}")
    else:
        print("No changes written.")


if __name__ == "__main__":
    main()
