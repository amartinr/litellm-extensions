"""
title: LiteLLM TimeRouter (Sticky Sessions)
id: time_router
author: A. Martin
author_url: https://github.com/amartinr
description: >
    LiteLLM custom pre-call hook (CustomLogger) for the alias
    `litellm/deepseek-v4-flash`. (1) Time-based routing: reroutes the alias
    between the two deployments declared in its
    `model_info.metadata.time_router.reroute` (peak_target / offpeak_target);
    the peak schedule is the provider fact declared as `peak_windows` on the
    offpeak_target entry (days 0=Mon..6=Sun, HH:MM UTC, interval [start, end);
    weekends are simply not listed). (2) Sticky sessions: pins each
    conversation (keyed on `metadata.session_id`, fed by the Open WebUI pipe's
    `x-litellm-session-id` header) to its provider while active, so long chats
    do not flip providers mid-conversation and lose the prompt cache. One-way
    ratchet while active (direct -> peak once in a peak window; peak stays
    through off-peak); idle beyond the TTL re-evaluates by the clock.
    (3) Labels requests with the config-declared route (`metadata.route` -> the
    `metadata_route` Prometheus label). Register as
    `time_router.proxy_handler_instance` under `litellm_settings.callbacks`.
    Config is read through the shared `hook_config` loader:
    per-model facts from `model_info.metadata`, operation knobs from the
    top-level `callback_settings.time_router` block (session_ttl_s,
    max_session_entries).
    Env knobs: TIME_ROUTER_DEBUG (verbose logs), TIME_ROUTER_FAKE_HOUR /
    TIME_ROUTER_FAKE_MINUTE (test window boundaries), TIME_ROUTER_FAKE_WEEKDAY
    (0=Mon..6=Sun, test weekends).
required_litellm_version: 1.99.0
version: 0.5.0
licence: MIT
"""

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# The hooks are mounted flat at /app, but load the shared module from this
# file's directory so `import hook_config` also works when the config (and
# therefore the hook source) lives outside the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_config
from litellm.integrations.custom_logger import CustomLogger

# Populated by _load_config() at import and refreshed by tests/reloads.
ROUTE_MAP: dict[str, str] = {}
ALIASES: dict[str, "AliasRoute"] = {}
SESSION_TTL_S = 900
MAX_SESSION_ENTRIES = 128
_session_state: dict[str, dict] = {}  # session_id -> {"alias", "route", "last_seen"}


@dataclass(frozen=True)
class AliasRoute:
    """Config-declared reroute decision for one alias model_name."""

    peak_target: str
    offpeak_target: str
    # (days, start, end) tuples from hook_config.parse_window; days 0=Mon..6=Sun,
    # start/end HH:MM UTC, interval [start, end).
    windows: tuple

    def is_peak(self, now: datetime) -> bool:
        clock = now.time()
        weekday = now.weekday()
        return any(weekday in days and start <= clock < end for days, start, end in self.windows)

    def target_at(self, now: datetime) -> str:
        return self.peak_target if self.is_peak(now) else self.offpeak_target


def _build_aliases(models: dict) -> dict[str, AliasRoute]:
    """Aliases are the entries declaring `time_router.reroute`. The schedule is
    read from the offpeak_target entry (the entry that owns the provider fact)."""
    aliases: dict[str, AliasRoute] = {}
    for name, desc in models.items():
        reroute = (desc.get("time_router") or {}).get("reroute")
        if not reroute:
            continue
        offpeak = reroute.get("offpeak_target")
        schedule = (models.get(offpeak, {}).get("time_router") or {}).get("peak_windows") or []
        windows = tuple(parsed for parsed in (hook_config.parse_window(raw) for raw in schedule) if parsed)
        aliases[name] = AliasRoute(
            peak_target=reroute.get("peak_target"),
            offpeak_target=offpeak,
            windows=windows,
        )
    return aliases


def _apply_config(loaded) -> None:
    global ROUTE_MAP, ALIASES, SESSION_TTL_S, MAX_SESSION_ENTRIES
    ROUTE_MAP = {name: desc["route"] for name, desc in loaded.models.items() if desc["route"]}
    ALIASES = _build_aliases(loaded.models)
    settings = loaded.settings("time_router")
    SESSION_TTL_S = settings["session_ttl_s"]
    MAX_SESSION_ENTRIES = settings["max_session_entries"]


def _load_config(path: str | None = None) -> None:
    _apply_config(hook_config.load(path))


_load_config()


def _log():
    """LiteLLM's proxy logger - shared loader handles the lazy import."""
    return hook_config.get_logger()


_last_warning_at = [0.0]
_WARNING_INTERVAL_S = 300.0


def _rate_limited_warning(msg: str, *args) -> None:
    """Emit through the proxy logger at most once per interval (fail-open)."""
    now = time.time()
    if now - _last_warning_at[0] < _WARNING_INTERVAL_S:
        return
    _last_warning_at[0] = now
    try:
        _log().warning(msg, *args)
    except Exception:
        pass


def _fake_int(name: str):
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _utc_now() -> datetime:
    """Now in UTC, with TIME_ROUTER_FAKE_* overrides for tests."""
    now = datetime.now(timezone.utc)
    fake_weekday = _fake_int("TIME_ROUTER_FAKE_WEEKDAY")
    if fake_weekday is not None:
        now += timedelta(days=(fake_weekday % 7 - now.weekday()) % 7)
    fake_hour = _fake_int("TIME_ROUTER_FAKE_HOUR")
    fake_minute = _fake_int("TIME_ROUTER_FAKE_MINUTE")
    if fake_hour is not None or fake_minute is not None:
        now = now.replace(
            hour=(fake_hour % 24) if fake_hour is not None else now.hour,
            minute=(fake_minute % 60) if fake_minute is not None else now.minute,
            second=0,
            microsecond=0,
        )
    return now


def _request_session_id(data: dict):
    md = data.get("metadata")
    if isinstance(md, dict) and isinstance(md.get("session_id"), str) and md["session_id"]:
        return md["session_id"]
    sid = data.get("litellm_session_id")
    return sid if isinstance(sid, str) and sid else None


class TimeRouter(CustomLogger):
    @staticmethod
    def _inject_route(data: dict, route: str) -> dict:
        """Write route where LiteLLM's Prometheus labels can read it.

        Custom labels come from the standard logging payload, which only keeps
        whitelisted metadata keys (StandardLoggingMetadata annotations) and copies
        `requester_metadata` by reference - a deepcopy snapshot taken by
        add_litellm_data_to_request BEFORE pre-call hooks run. Top-level metadata
        keys are dropped, so the value must go into requester_metadata (and
        spend_logs_metadata, read by spend-log consumers).
        """
        metadata = data.setdefault("metadata", {})
        metadata["route"] = route                                    # request-level, debug/other consumers
        metadata.setdefault("requester_metadata", {})["route"] = route   # -> metadata_route label
        metadata.setdefault("spend_logs_metadata", {})["route"] = route  # -> spend logs
        return metadata

    # ------------------------------------------------------------------ policy
    def _pick_route(self, alias: str, route: AliasRoute, session_id, now: datetime) -> str:
        """Sticky-session route decision for alias traffic.

        - No session id            -> stateless hour routing (title gen etc.)
        - New / idle session       -> hour routing, then pinned
        - Active session pinned to the offpeak target crossing INTO peak
                                   -> switch to the peak target ONCE
        - Active session pinned to the peak target crossing INTO off-peak
                                   -> STAY (keep the warm provider cache;
                                      one-way ratchet while active)
        """
        desired = route.target_at(now)
        if not session_id:
            return desired
        state = _session_state.get(session_id)
        if state is None or state.get("alias") != alias:
            return desired
        if (time.time() - state["last_seen"]) >= SESSION_TTL_S:
            # Idle long enough that the provider cache is cold: re-evaluate.
            return desired
        pinned = state["route"]
        if pinned == route.offpeak_target:
            return route.peak_target if desired == route.peak_target else route.offpeak_target
        return pinned  # pinned to the peak target: stay while active

    @staticmethod
    def _prune_sessions(now: float) -> None:
        if len(_session_state) <= MAX_SESSION_ENTRIES:
            return
        expired = [k for k, v in _session_state.items() if (now - v["last_seen"]) >= SESSION_TTL_S]
        for k in expired:
            _session_state.pop(k, None)
        overflow = len(_session_state) - MAX_SESSION_ENTRIES
        if overflow > 0:  # hard cap: evict the least recently seen
            oldest = sorted(_session_state, key=lambda k: _session_state[k]["last_seen"])[:overflow]
            for k in oldest:
                _session_state.pop(k, None)

    # ------------------------------------------------------------------ hook
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            requested = data.get("model")
            route_label = None

            if requested in ALIASES:
                route = ALIASES[requested]
                now = _utc_now()
                session_id = _request_session_id(data)
                target = self._pick_route(requested, route, session_id, now)
                seen = time.time()
                if session_id:
                    _session_state[session_id] = {"alias": requested, "route": target, "last_seen": seen}
                    self._prune_sessions(seen)
                data["model"] = target
                route_label = ROUTE_MAP.get(target, "unknown")
                # Always-on lightweight signal (rare): only when stickiness overrode
                # the clock, i.e. an ACTIVE session crossed a window boundary.
                clock_target = route.target_at(now)
                if session_id and target != clock_target:
                    _log().info(
                        "TimeRouter: STICKY session=%s… kept/switch to %s (clock says %s)",
                        session_id[:12],
                        target,
                        clock_target,
                    )
                if os.environ.get("TIME_ROUTER_DEBUG"):
                    _log().info(
                        "TimeRouter: requested=%r target=%r route=%s session=%r",
                        requested,
                        target,
                        route_label,
                        session_id,
                    )
            elif requested in ROUTE_MAP:
                # Direct call to a model that declares a route in config.yaml:
                # label it with the declared route, no rerouting.
                route_label = ROUTE_MAP[requested]
                if os.environ.get("TIME_ROUTER_DEBUG"):
                    _log().info(
                        "TimeRouter: requested=%r target=None route=%s",
                        requested,
                        route_label,
                    )

            if route_label is not None:
                metadata = self._inject_route(data, route_label)
                if os.environ.get("TIME_ROUTER_DEBUG"):
                    import json as _json

                    _log().info("TimeRouter: ROUTE_MAP=%s", _json.dumps(ROUTE_MAP))
                    _log().info(
                        "TimeRouter: metadata after inject=%s",
                        _json.dumps(metadata, default=str),
                    )
                    _log().info("TimeRouter: data keys=%s", sorted(data.keys()))
                    _md = data.get("metadata")
                    if isinstance(_md, dict):
                        _log().info("TimeRouter: metadata keys=%s", sorted(_md.keys()))
                        for _k in ("session_id", "chat_id", "litellm_session_id", "litellm_trace_id", "user_id"):
                            if _k in _md:
                                _v = str(_md[_k])
                                _log().info(
                                    "TimeRouter: metadata[%s]=%s%s",
                                    _k,
                                    _v[:80],
                                    "…" if len(_v) > 80 else "",
                                )
                    for _k in ("session_id", "chat_id", "litellm_session_id", "litellm_trace_id", "user"):
                        if _k in data:
                            _v = str(data[_k])
                            _log().info(
                                "TimeRouter: data[%s]=%s%s",
                                _k,
                                _v[:80],
                                "…" if len(_v) > 80 else "",
                            )
        except Exception as exc:  # fail-open: never break the request
            _rate_limited_warning("TimeRouter: hook failed, request left unchanged: %r", exc)
        return data


proxy_handler_instance = TimeRouter()
