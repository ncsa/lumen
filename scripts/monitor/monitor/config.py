"""Load and validate the monitor's config.yaml.

`load_config()` raises `ConfigError` for a config the service cannot run with
(bad durations, `refresh` values out of order, an unknown timezone). Problems
that only disable part of the service (an unknown GPU type, a missing `kwh`,
a short `retention`) are collected in `Config.warnings` and logged, so the
probes keep running while the hardware cost for that model is skipped.

`ConfigStore` holds the current config and re-reads the file on `reload()`;
a reload that fails keeps the previous config.
"""

import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yaml"

# An empty MONITOR_DATABASE_URL or MONITOR_LUMEN_API_KEY counts as unset. An empty
# MONITOR_BASE_PATH still overrides the file on purpose: "" means "served at the root".
ENV_DATABASE_URL = "MONITOR_DATABASE_URL"
ENV_BASE_PATH = "MONITOR_BASE_PATH"
ENV_LUMEN_API_KEY = "MONITOR_LUMEN_API_KEY"

DEFAULT_REFRESH = {"web": "1m", "models": "5m", "price": "1h", "page": "5m", "timeout": "15s"}
DEFAULT_RETENTION = "31d"
MIN_RETENTION = timedelta(days=30)
MAX_MODELS_REFRESH = timedelta(hours=1)
# Upper bound for any duration; also keeps timedelta() from overflowing.
MAX_DURATION = timedelta(days=3650)
# Lumen counts as down when more than this percent of its models are down.
DEFAULT_DOWN_ABOVE = 50.0
TOP_LEVEL_KEYS = {"database_url", "base_path", "timezone", "retention", "refresh", "lumen", "price", "models"}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class ConfigError(ValueError):
    """The config cannot be used."""


@dataclass(frozen=True)
class Refresh:
    web: timedelta
    models: timedelta
    price: timedelta
    page: timedelta
    timeout: timedelta


@dataclass(frozen=True)
class Gpu:
    energy: float  # watts per GPU
    price: float  # $ per GPU per month


@dataclass(frozen=True)
class ModelHardware:
    gpu: str | None
    gpus: int | None
    modelsdev_id: str | None = None
    lumen_id: str | None = None


@dataclass(frozen=True)
class Config:
    database_url: str
    base_path: str
    timezone: ZoneInfo
    retention: timedelta
    refresh: Refresh
    lumen_url: str
    lumen_api_key: str
    kwh: float | None
    gpus: dict[str, Gpu]
    models: dict[str, ModelHardware]
    warnings: list[str] = field(default_factory=list, compare=False)
    down_above: float = DEFAULT_DOWN_ABOVE  # lumen.down_above, percent of models

    def model_entry(self, model: str) -> ModelHardware | None:
        """The `models:` entry for Lumen id `model`: the entry pinned to it with `lumen_id`, else
        the exact key, else the key with the same `model_key`. Keys of pinned entries never match."""
        pinned = next((entry for entry in self.models.values() if entry.lumen_id == model), None)
        if pinned is not None:
            return pinned
        unpinned = {name: entry for name, entry in self.models.items() if entry.lumen_id is None}
        if model in unpinned:
            return unpinned[model]
        key = model_key(model)
        return next((entry for name, entry in unpinned.items() if model_key(name) == key), None)

    def match_models(self, models: list[str]) -> dict[str, ModelHardware]:
        """The `models:` entry of each Lumen id that has one.

        An id without a pin or an exact key that shares its `model_key` with another id in
        `models` gets no entry (one warning per clash): guessing could charge the wrong
        GPUs. Its `modelsdev_id` pin is dropped with the entry, so its models.dev match
        falls back to the fuzzy matcher. A `lumen_id` that names none of `models` is
        warned about; its entry is unused.
        """
        for name, entry in self.models.items():
            if entry.lumen_id is not None and entry.lumen_id not in models:
                log.warning("models.%s: lumen_id %r matches no Lumen model; entry unused", name, entry.lumen_id)
        exact = {entry.lumen_id for entry in self.models.values() if entry.lumen_id is not None}
        exact.update(name for name, entry in self.models.items() if entry.lumen_id is None)
        keys: dict[str, set[str]] = {}
        for model in models:
            keys.setdefault(model_key(model), set()).add(model)
        matched = {}
        for key, ids in keys.items():
            skipped = []
            for model in sorted(ids):
                entry = self.model_entry(model)
                if entry is None:
                    continue
                if model not in exact and len(ids) > 1:
                    skipped.append(model)
                    continue
                matched[model] = entry
            if skipped:
                log.warning("models: Lumen ids %s share the key %r; no hardware cost for %s", ", ".join(sorted(ids)), key, ", ".join(skipped))
        return matched

    def entry_hardware(self, entry: ModelHardware | None) -> tuple[Gpu, int] | None:
        """The GPU type and count of a `models:` entry, or None when its cost cannot be computed."""
        if self.kwh is None or entry is None or entry.gpus is None or entry.gpu not in self.gpus:
            return None
        return self.gpus[entry.gpu], entry.gpus

    def hardware_for(self, model: str) -> tuple[Gpu, int] | None:
        """The GPU type and count for Lumen id `model`, or None when its cost cannot be computed."""
        return self.entry_hardware(self.model_entry(model))


def model_key(name: str) -> str:
    """How a `models:` key meets a Lumen id: `deepseek-v4-1-flash` configures `deepseek-v4.1-flash`,
    since dotted YAML keys need quoting."""
    return name.strip().lower().replace(".", "-")


def parse_duration(value, name: str) -> timedelta:
    """Parse `15s`, `5m`, `1h`, `31d` or a bare number of seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"{name}: expected a duration like 5m, got {value!r}")
    try:
        if isinstance(value, (int, float)):
            seconds = float(value)
        else:
            match = _DURATION_RE.match(str(value))
            if not match:
                raise ConfigError(f"{name}: expected a duration like 5m, got {value!r}")
            seconds = float(match.group(1)) * _UNITS[match.group(2)]
    except OverflowError:
        seconds = math.inf
    if not math.isfinite(seconds) or seconds > MAX_DURATION.total_seconds():
        raise ConfigError(f"{name}: must be a finite duration of at most {MAX_DURATION.days}d, got {value!r}")
    if seconds <= 0:
        raise ConfigError(f"{name}: must be greater than zero")
    return timedelta(seconds=seconds)


def normalize_base_path(value) -> str:
    """`monitor/` → `/monitor`; empty or `/` → `` (served at the root)."""
    path = str(value or "").strip().strip("/")
    return f"/{path}" if path else ""


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: expected a mapping")
    return value


def _number(value, name: str, warnings: list[str]) -> float | None:
    try:
        number = None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)
    except OverflowError:
        number = None
    if number is None or not math.isfinite(number) or number < 0:
        warnings.append(f"{name}: expected a finite non-negative number, got {value!r}")
        return None
    return number


def _parse_down_above(lumen: dict, warnings: list[str]) -> float:
    """`lumen.down_above`: a percent from 0 to 100; anything else warns and falls back to the default."""
    value = lumen.get("down_above")
    if value is None:
        return DEFAULT_DOWN_ABOVE
    try:
        number = None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)
    except OverflowError:
        number = None
    if number is None or not 0 <= number <= 100:
        warnings.append(f"lumen.down_above: expected a number from 0 to 100, got {value!r}; using {DEFAULT_DOWN_ABOVE:g}")
        return DEFAULT_DOWN_ABOVE
    return number


def _parse_refresh(raw: dict) -> Refresh:
    # An empty value (YAML null) means "use the default", as for every other key.
    section = {**DEFAULT_REFRESH, **{k: v for k, v in _section(raw, "refresh").items() if v is not None}}
    unknown = sorted(set(section) - set(DEFAULT_REFRESH))
    if unknown:
        raise ConfigError(f"refresh: unknown keys {', '.join(unknown)}")
    refresh = Refresh(**{key: parse_duration(section[key], f"refresh.{key}") for key in DEFAULT_REFRESH})
    if not refresh.timeout < refresh.web:
        raise ConfigError("refresh: timeout must be shorter than web")
    if not refresh.web <= refresh.models <= MAX_MODELS_REFRESH:
        raise ConfigError("refresh: models must be at least web and at most 1h")
    if not refresh.web <= refresh.price:
        raise ConfigError("refresh: price must be at least web")
    return refresh


def _parse_price(raw: dict, warnings: list[str]) -> tuple[float | None, dict[str, Gpu]]:
    section = _section(raw, "price")
    kwh = None
    if "kwh" in section:
        kwh = _number(section["kwh"], "price.kwh", warnings)
    gpus = {}
    for name, entry in section.items():
        if name == "kwh":
            continue
        if not isinstance(entry, dict):
            warnings.append(f"price.{name}: expected a mapping with energy and price")
            continue
        energy = _number(entry.get("energy"), f"price.{name}.energy", warnings)
        price = _number(entry.get("price"), f"price.{name}.price", warnings)
        if energy is not None and price is not None:
            gpus[str(name)] = Gpu(energy=energy, price=price)
    if kwh is None and "kwh" not in section and (gpus or raw.get("models")):
        warnings.append("price.kwh is missing: no hardware cost is computed")
    return kwh, gpus


def _parse_models(raw: dict, gpus: dict[str, Gpu], warnings: list[str]) -> dict[str, ModelHardware]:
    models = {}
    seen: dict[str, str] = {}
    for name, entry in _section(raw, "models").items():
        key = model_key(str(name))
        if key in seen:
            raise ConfigError(f"models: {seen[key]!r} and {name!r} configure the same model (dots and dashes are the same)")
        seen[key] = str(name)
        if not isinstance(entry, dict):
            warnings.append(f"models.{name}: expected a mapping with gpu and gpus")
            continue
        gpu = entry.get("gpu")
        count = entry.get("gpus")
        if gpu is None:
            warnings.append(f"models.{name}: gpu is missing")
        elif str(gpu) not in gpus:
            warnings.append(f"models.{name}: unknown GPU type {gpu!r} (not under price:)")
        if count is None:
            warnings.append(f"models.{name}: gpus is missing")
        elif isinstance(count, bool) or not isinstance(count, int) or count < 1:
            warnings.append(f"models.{name}: gpus must be a positive integer, got {count!r}")
            count = None
        modelsdev_id = entry.get("modelsdev_id")
        lumen_id = entry.get("lumen_id")
        if lumen_id is not None and not (isinstance(lumen_id, str) and lumen_id.strip()):
            warnings.append(f"models.{name}: lumen_id must be a non-empty string, got {lumen_id!r}; ignored")
            lumen_id = None
        models[str(name)] = ModelHardware(
            gpu=None if gpu is None else str(gpu),
            gpus=count,
            modelsdev_id=None if modelsdev_id is None else str(modelsdev_id),
            lumen_id=None if lumen_id is None else lumen_id.strip(),
        )
    pins: dict[str, str] = {}
    for name, entry in models.items():
        if entry.lumen_id is None:
            continue
        if entry.lumen_id in pins:
            raise ConfigError(f"models: {pins[entry.lumen_id]!r} and {name!r} have the same lumen_id {entry.lumen_id!r}")
        pins[entry.lumen_id] = name
        other = next((key for key in seen.values() if key != name and model_key(key) == model_key(entry.lumen_id)), None)
        if other is not None:
            raise ConfigError(f"models: lumen_id {entry.lumen_id!r} of {name!r} configures the same model as {other!r}")
    return models


def parse_config(raw: dict | None, env: dict | None = None) -> Config:
    """Build a `Config` from parsed YAML plus environment overrides."""
    env = os.environ if env is None else env
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("config: expected a mapping at the top level")
    warnings: list[str] = []
    unknown = sorted(str(key) for key in set(raw) - TOP_LEVEL_KEYS)
    if unknown:
        warnings.append(f"unknown top-level keys ignored: {', '.join(unknown)}")

    tz_name = str(raw.get("timezone") or "UTC")
    try:
        timezone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone: unknown timezone {tz_name!r}") from exc

    retention = parse_duration(DEFAULT_RETENTION if raw.get("retention") is None else raw["retention"], "retention")
    if retention < MIN_RETENTION:
        warnings.append("retention is shorter than 30d: the 30d view will be cut short")

    lumen = _section(raw, "lumen")
    down_above = _parse_down_above(lumen, warnings)
    kwh, gpus = _parse_price(raw, warnings)
    models = _parse_models(raw, gpus, warnings)

    return Config(
        database_url=env.get(ENV_DATABASE_URL) or str(raw.get("database_url") or "sqlite:///monitor.db"),
        base_path=normalize_base_path(env.get(ENV_BASE_PATH, raw.get("base_path"))),
        timezone=timezone,
        retention=retention,
        refresh=_parse_refresh(raw),
        lumen_url=str(lumen.get("url") or "").rstrip("/"),
        lumen_api_key=env.get(ENV_LUMEN_API_KEY) or str(lumen.get("api_key") or ""),
        kwh=kwh,
        gpus=gpus,
        models=models,
        warnings=warnings,
        down_above=down_above,
    )


def load_config(path: str | os.PathLike = DEFAULT_CONFIG_PATH, env: dict | None = None) -> Config:
    """Read and validate `path`; a missing file means all defaults."""
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raw = {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    config = parse_config(raw, env)
    for warning in config.warnings:
        log.warning("config: %s", warning)
    return config


class ConfigStore:
    """The current config, re-read from disk on `reload()`."""

    def __init__(self, path: str | os.PathLike = DEFAULT_CONFIG_PATH, env: dict | None = None):
        self.path = path
        self.env = env
        self.current = load_config(path, env)

    def reload(self) -> bool:
        """Re-read the file. On error, log it, keep the old config and return False."""
        try:
            self.current = load_config(self.path, self.env)
        except ConfigError as exc:
            log.error("config reload failed, keeping the previous config: %s", exc)
            return False
        return True
