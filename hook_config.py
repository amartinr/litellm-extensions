"""hook_config.py — shared configuration loader for the LiteLLM hooks.

`time_router` and `reasoning_route_adapter` both read `config.yaml`. This
module centralizes that: it locates the file, builds a per-model descriptor
map from `model_info.metadata` (`route`, `reasoning_dialect`, `time_router`),
reads the top-level `callback_settings` block, and validates the routing
config.

Design notes:
- `load()` is called by each hook at module level, *not* cached here. A config
  reload re-executes the hook module (LiteLLM's `get_instance_fn`), so calling
  `load()` there also refreshes this data. See DESIGN.md §5.2.
- Config errors never abort proxy startup: unreadable YAML yields empty
  models; a semantically invalid `time_router` block disables rerouting and
  logs ERROR per problem.
- Mounted at `/app/hook_config.py` and imported as top-level `hook_config`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import time as dtime
from typing import Any

import yaml

CONFIG_PATHS = [
    os.environ.get("LITELLM_CONFIG_FILE", ""),  # env var wins if set
    "/app/config.yaml",                         # real path
    "./config.yaml",
]

DAYS_PER_WEEK = 7

# Operation-knob defaults. Single source: SETTINGS_DEFAULTS below and the
# hooks' fallback constants reference these names.
DEFAULT_SESSION_TTL_S = 900
DEFAULT_MAX_SESSION_ENTRIES = 128
DEFAULT_WARNING_INTERVAL_S = 300

# callback_settings.<hook> defaults. Applied when the block or a value is
# absent or has the wrong type (the latter also logs a warning).
SETTINGS_DEFAULTS: dict[str, dict[str, Any]] = {
    "time_router": {
        "session_ttl_s": DEFAULT_SESSION_TTL_S,
        "max_session_entries": DEFAULT_MAX_SESSION_ENTRIES,
    },
    "reasoning_route_adapter": {
        "warning_interval_s": DEFAULT_WARNING_INTERVAL_S,
        "native_route_labels": ["deepseek"],
        "or_route_labels": ["baidu/fp8"],
    },
}

SETTINGS_TYPES: dict[str, dict[str, Any]] = {
    "time_router": {"session_ttl_s": int, "max_session_entries": int},
    "reasoning_route_adapter": {
        "warning_interval_s": (int, float),
        "native_route_labels": list,
        "or_route_labels": list,
    },
}


def get_logger():
    """LiteLLM's proxy logger, with a stdlib fallback for offline tests."""
    try:
        from litellm.proxy.proxy_server import verbose_proxy_logger

        return verbose_proxy_logger
    except Exception:
        pass
    try:
        from litellm import verbose_logger

        return verbose_logger
    except Exception:
        return logging.getLogger("hook_config")


def parse_window(raw: Any) -> tuple[frozenset[int], dtime, dtime] | None:
    """(days, start, end) for a peak window, or None if invalid.

    `days` uses ``datetime.weekday()`` (0=Mon..6=Sun); ``start``/``end`` are
    ``HH:MM`` UTC; the interval is ``[start, end)``.
    """
    try:
        days = frozenset(int(day) for day in raw["days"])
        start = dtime.fromisoformat(str(raw["start"]))
        end = dtime.fromisoformat(str(raw["end"]))
    except Exception:
        return None
    if not days or not days <= set(range(DAYS_PER_WEEK)) or start >= end:
        return None
    return days, start, end


def validate_models(models: dict[str, dict]) -> list[str]:
    """Errors in the routing config; empty when valid."""
    errors: list[str] = []
    for name, desc in models.items():
        reroute = (desc.get("time_router") or {}).get("reroute")
        if not reroute:
            continue
        for key in ("peak_target", "offpeak_target"):
            if reroute.get(key) not in models:
                errors.append(f"{name}: reroute.{key}={reroute.get(key)!r} not in model_list")
        offpeak = models.get(reroute.get("offpeak_target"), {})
        windows = (offpeak.get("time_router") or {}).get("peak_windows")
        if not windows:
            errors.append(f"{name}: offpeak_target {reroute.get('offpeak_target')!r} has no peak_windows")
            continue
        for raw in windows:
            if parse_window(raw) is None:
                errors.append(f"{name}: invalid peak window {raw!r}")
    return errors


def parse_config(cfg: dict) -> tuple[dict[str, dict], dict]:
    """(models, raw callback_settings) from a parsed config. Pure, no I/O."""
    models: dict[str, dict] = {}
    for entry in cfg.get("model_list") or []:
        name = entry.get("model_name")
        if not name:
            continue
        meta = (entry.get("model_info") or {}).get("metadata") or {}
        models[name] = {
            "route": meta.get("route"),
            "reasoning_dialect": meta.get("reasoning_dialect"),
            "time_router": meta.get("time_router") or {},
        }

    errors = validate_models(models)
    if errors:
        logger = get_logger()
        for err in errors:
            logger.error("hook_config: %s", err)
        # Disable rerouting (keep route labeling) rather than send a bad model.
        for desc in models.values():
            (desc.get("time_router") or {}).pop("reroute", None)

    raw = cfg.get("callback_settings")
    return models, raw if isinstance(raw, dict) else {}


def _type_ok(value: Any, expected: Any) -> bool:
    if expected is int and isinstance(value, bool):
        return False  # bool is an int subclass; reject it for int knobs
    return isinstance(value, expected)


def settings_for(raw_settings: dict, hook: str) -> dict:
    """``callback_settings.<hook>`` with defaults applied and type checks."""
    defaults = SETTINGS_DEFAULTS.get(hook, {})
    block = (raw_settings or {}).get(hook) or {}
    if not isinstance(block, dict):
        get_logger().warning("hook_config: callback_settings.%s is not a dict -> defaults", hook)
        block = {}
    out = dict(defaults)
    for key, expected in SETTINGS_TYPES.get(hook, {}).items():
        if key not in block:
            continue
        if _type_ok(block[key], expected):
            out[key] = block[key]
        else:
            get_logger().warning(
                "hook_config: callback_settings.%s.%s has wrong type -> default %r",
                hook,
                key,
                defaults.get(key),
            )
    return out


@dataclass(frozen=True)
class Loaded:
    """Result of :func:`load`: per-model descriptors + raw callback_settings."""

    models: dict[str, dict]
    raw_settings: dict

    def settings(self, hook: str) -> dict:
        return settings_for(self.raw_settings, hook)


def find_config_path(paths: list[str] | None = None) -> str | None:
    for path in paths if paths is not None else CONFIG_PATHS:
        if path and os.path.exists(path):
            return path
    return None


def load(path: str | None = None) -> Loaded:
    """Read ``config.yaml`` and return a :class:`Loaded`. Never raises for
    config errors."""
    path = path or find_config_path()
    if not path:
        get_logger().warning(
            "hook_config: no config file found (tried %r) -> hooks no-op", CONFIG_PATHS
        )
        return Loaded({}, {})
    try:
        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:
        get_logger().error("hook_config: cannot read %s: %r -> hooks no-op", path, exc)
        return Loaded({}, {})
    models, raw = parse_config(cfg)
    return Loaded(models, raw)
