"""Read Lumen's `GET /v1/models`.

Only canonical models are kept: alias rows (`parent` set) repeat their
parent's data. `tokens` and the `last_month` windows are newer fields, so they
are read only when the response has them and are None otherwise.

The probe asks for `tokens=true`. Lumen answers it only for the monitoring
token, adding `input_tokens` / `output_tokens` windows; any other key, or an
older Lumen, ignores the parameter and those fields are None.
"""

import requests

WINDOWS = ("last_minute", "last_hour", "last_day", "last_month")


def _windows(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in WINDOWS}


def _float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_models(payload: dict) -> list[dict]:
    """Canonical models from a `/v1/models` response body."""
    if not isinstance(payload, dict):
        raise ValueError("/v1/models: expected a JSON object")
    models = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict) or row.get("parent") or not row.get("id"):
            continue
        instances = row.get("instances") or {}
        models.append({
            "id": row["id"],
            "root": row.get("root"),
            "description": row.get("description"),
            "status": row.get("status"),
            "configured": instances.get("configured"),
            "healthy": instances.get("healthy"),
            "input_price": _float(row.get("input_cost_per_million")),
            "output_price": _float(row.get("output_cost_per_million")),
            "requests": _windows(row.get("requests")),
            "tokens": _windows(row.get("tokens")),
            "input_tokens": _windows(row.get("input_tokens")),
            "output_tokens": _windows(row.get("output_tokens")),
        })
    return models


def fetch_models(url: str, api_key: str, timeout: float) -> list[dict]:
    """GET `{url}/v1/models?tokens=true`; raises `requests.RequestException` or `ValueError` on failure."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = requests.get(f"{url}/v1/models", params={"tokens": "true"}, headers=headers, timeout=timeout)
    r.raise_for_status()
    return parse_models(r.json())
