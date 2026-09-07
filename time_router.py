"""
title: LiteLLM TimeRouter (Sticky Sessions)
id: time_router
author: A. Martin
author_url: https://github.com/amartinr
description: >
    LiteLLM custom pre-call hook (CustomLogger) for the alias
    `litellm/deepseek-v4-flash`. (1) Time-based routing: DeepSeek peak windows
    01:00-04:00 and 06:00-10:00 UTC reroute to `openrouter/deepseek-v4-flash`,
    otherwise to `deepseek/deepseek-v4-flash`. (2) Sticky sessions: pins each
    conversation (keyed on `metadata.session_id`, fed by the Open WebUI pipe's
    `x-litellm-session-id` header) to its provider while active, so long chats do
    not flip providers mid-conversation and lose the prompt cache. One-way
    ratchet while active (direct -> openrouter once at peak; openrouter stays
    through off-peak); idle beyond the TTL re-evaluates by the clock.
    (3) Labels requests with the config-declared route (`metadata.route` -> the
    `metadata_route` Prometheus label). Register as
    `time_router.proxy_handler_instance` under `litellm_settings.callbacks`.
    Env knobs: TIME_ROUTER_DEBUG (verbose logs), TIME_ROUTER_FAKE_HOUR (test
    window boundaries), TIME_ROUTER_SESSION_TTL (idle TTL, default 900 s).
required_litellm_version: 1.99.0
version: 0.4.1
licence: MIT
"""

import os
import time
from datetime import datetime, timezone
import yaml
from litellm.integrations.custom_logger import CustomLogger

CONFIG_PATHS = [
    os.environ.get("LITELLM_CONFIG_FILE", ""),   # env var wins if set
    "/app/config.yaml",                          # real path
    "./config.yaml",
]

ALIAS_MODEL = "litellm/deepseek-v4-flash"
PEAK_TARGET = "openrouter/deepseek-v4-flash"
OFFPEAK_TARGET = "deepseek/deepseek-v4-flash"

# Sticky-session policy -------------------------------------------------------
# After this much inactivity the provider-side prompt cache is presumed cold,
# so the pin is dropped and routing re-evaluates by the clock.
SESSION_IDLE_TTL_S = int(os.environ.get("TIME_ROUTER_SESSION_TTL", "900"))  # 15 min
_session_state: dict[str, dict] = {}  # session_id -> {"route": str, "last_seen": float}


def _load_route_map():
    """Builds {model_name: route} from model_info.metadata.route in config.yaml."""
    for p in CONFIG_PATHS:
        if p and os.path.exists(p):
            with open(p) as f:
                cfg = yaml.safe_load(f)
            route_map = {}
            for m in (cfg.get("model_list") or []):
                name = m.get("model_name")
                route = (m.get("model_info") or {}).get("metadata", {}).get("route")
                if name and route:
                    route_map[name] = route
            return route_map
    return {}


ROUTE_MAP = _load_route_map()


def _utc_hour() -> int:
    # TIME_ROUTER_FAKE_HOUR lets tests exercise window boundaries on demand.
    fake = os.environ.get("TIME_ROUTER_FAKE_HOUR")
    if fake:
        try:
            return int(fake) % 24
        except ValueError:
            pass
    return datetime.now(timezone.utc).hour


def _is_peak(hour: int) -> bool:
    # DeepSeek peak windows: 01:00-04:00 and 06:00-10:00 UTC
    return (1 <= hour < 4) or (6 <= hour < 10)


def _desired_route(hour: int) -> str:
    return PEAK_TARGET if _is_peak(hour) else OFFPEAK_TARGET


def _request_session_id(data: dict):
    md = data.get("metadata")
    if isinstance(md, dict) and md.get("session_id"):
        return md["session_id"]
    return data.get("litellm_session_id")


def _log():
    """LiteLLM's proxy logger - emits JSON lines when `json_logs` is on.

    Lazy import: the hook module is imported early by the proxy; importing
    proxy_server at module level would risk an import cycle.
    """
    try:
        from litellm.proxy.proxy_server import verbose_proxy_logger

        return verbose_proxy_logger
    except Exception:
        from litellm import verbose_logger

        return verbose_logger


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
    def _pick_route(self, session_id, hour: int) -> str:
        """Sticky-session route decision for alias traffic.

        - No session id            -> stateless hour routing (title gen etc.)
        - New / idle session       -> hour routing, then pinned
        - Active session pinned to DIRECT crossing INTO peak
                                   -> switch to OpenRouter ONCE (avoid DeepSeek
                                      peak prices for the whole active stretch)
        - Active session pinned to OPENROUTER crossing INTO off-peak
                                   -> STAY on OpenRouter (keep the warm Baidu
                                      cache; skipping credit burn is the lesser
                                      evil). One-way ratchet while active.
        """
        desired = _desired_route(hour)
        if not session_id:
            return desired
        state = _session_state.get(session_id)
        if state is None:
            return desired
        if (time.time() - state["last_seen"]) >= SESSION_IDLE_TTL_S:
            # Idle long enough that the provider cache is cold: re-evaluate.
            return desired
        pinned = state["route"]
        if pinned == OFFPEAK_TARGET:
            return PEAK_TARGET if desired == PEAK_TARGET else OFFPEAK_TARGET
        return pinned  # on OpenRouter (peak or off-peak): stay while active

    @staticmethod
    def _prune_sessions(now: float, max_entries: int = 128) -> None:
        if len(_session_state) <= max_entries:
            return
        expired = [k for k, v in _session_state.items() if (now - v["last_seen"]) >= SESSION_IDLE_TTL_S]
        for k in expired:
            _session_state.pop(k, None)

    # ------------------------------------------------------------------ hook
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        requested = data.get("model")
        route_label = None

        if requested == ALIAS_MODEL:
            hour = _utc_hour()
            session_id = _request_session_id(data)
            route = self._pick_route(session_id, hour)
            now = time.time()
            if session_id:
                _session_state[session_id] = {"route": route, "last_seen": now}
                self._prune_sessions(now)
            data["model"] = route
            route_label = ROUTE_MAP.get(route, "unknown")
            # Always-on lightweight signal (rare): only when stickiness overrode
            # the clock, i.e. an ACTIVE session crossed a window boundary.
            clock_route = _desired_route(hour)
            if session_id and route != clock_route:
                _log().info(
                    "TimeRouter: STICKY session=%s… kept/switch to %s (clock says %s)",
                    session_id[:12],
                    route,
                    clock_route,
                )
            if os.environ.get("TIME_ROUTER_DEBUG"):
                _log().info(
                    "TimeRouter: requested=%r target=%r route=%s hour=%s session=%r",
                    requested,
                    route,
                    route_label,
                    hour,
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
        return data


proxy_handler_instance = TimeRouter()
