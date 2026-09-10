"""
title: LiteLLM Reasoning Route Adapter (per-route reasoning-dialect normalization)
id: reasoning_route_adapter
author: A. Martin
author_url: https://github.com/amartinr
description: >
    LiteLLM custom pre-call hook (CustomLogger), registered AFTER
    `time_router.proxy_handler_instance` in `litellm_settings.callbacks` so
    `data["model"]` is the post-reroute model. It normalizes the reasoning
    control of the request to the dialect that the bound route honors.
    Route classification is config-driven: each model_list entry declares
    `model_info.metadata.reasoning_dialect` ("deepseek" -> native-bound,
    "openrouter" -> OR-bound); entries without the declaration fall back to
    the route-label taxonomy below.
    Clients (Open WebUI and pi, each with an extension) send the
    DeepSeek-native dialect - root keys `thinking:{type:enabled|disabled}` and
    `reasoning_effort` (the raw HTTP contract of api.deepseek.com, see its
    curl example - the OpenAI-SDK `extra_body` wrapper is an SDK artifact, not
    the wire format LiteLLM forwards). Evidence: probes/, 2026-09-08; DESIGN.md.

    OR-bound (route label "baidu/fp8", OpenRouter -> Baidu fp8):
      - `thinking:{type:"disabled"}` is IGNORED by OR (probed: still reasoned,
        47-57 tokens) -> rescued into the OR contract OFF
        `reasoning:{enabled:false, effort:"none"}` (0 tokens); `thinking` and
        any root `reasoning_effort` are dropped.
      - `thinking:{type:"enabled"}` is ignored too -> dropped only (OR default
        is reasoning ON; never synthesize a reasoning object for ON: object
        effort over-spends on this route while root `reasoning_effort` is
        cheap - probes 2026-09-08).
      - an OR `reasoning` object and root `reasoning_effort` pass through
        (already the OR dialect / cheap on OR).
      - assistant-message `reasoning_content` is never touched (documented OR
        alias for its `reasoning` field).

    Native-bound (route label "deepseek", api.deepseek.com):
      - the native dialect (`thinking` / root `reasoning_effort`) passes
        through untouched (honored natively; the thinking-mode docs allow both
        keys in one request).
      - an incoming OR `reasoning` object (probe/future client only) is
        translated: OFF -> `thinking:{type:"disabled"}`; ON ->
        `thinking:{type:"enabled"}` + root `reasoning_effort` mapped through
        the DeepSeek collapse table (low/high/max pass through, medium/xhigh
        -> high, minimal -> low; unmappable -> omit the effort key).

    Never touches `messages`. Deterministic, idempotent (re-running over an
    already-normalized payload is a no-op), fail-open (any error leaves the
    request unchanged, warned at most once per 5 min), stateless, no I/O.
    Rollback: REASONING_ADAPTER_DISABLED=1 (checked per request).
    Env knobs: REASONING_ADAPTER_DEBUG (verbose logs incl. before/after
    reasoning keys), REASONING_ADAPTER_NATIVE_ROUTES / REASONING_ADAPTER_OR_ROUTES
    (comma-separated route-label sets, defaults deepseek / baidu/fp8 - only
    the fallback taxonomy for entries without a declared dialect).
required_litellm_version: 1.99.0
version: 0.2.1
licence: MIT
"""

import json as _json
import os
import time

import yaml
from litellm.integrations.custom_logger import CustomLogger

CONFIG_PATHS = [
    os.environ.get("LITELLM_CONFIG_FILE", ""),   # env var wins if set
    "/app/config.yaml",                          # real path
    "./config.yaml",
]

# Fallback dialect taxonomy - used only for model_list entries that do NOT
# declare model_info.metadata.reasoning_dialect in config.yaml (the declared
# dialect is the primary classification source). A route label outside both
# sets is treated as unknown -> no-op (extend via env if new labels appear).
def _label_set(env_name: str, default: list) -> set:
    raw = os.environ.get(env_name)
    if raw:
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set(default)


NATIVE_ROUTE_LABELS = _label_set("REASONING_ADAPTER_NATIVE_ROUTES", ["deepseek"])
OR_ROUTE_LABELS = _label_set("REASONING_ADAPTER_OR_ROUTES", ["baidu/fp8"])

# OR-object effort -> native root `reasoning_effort`. DeepSeek's own collapse
# table (low/medium/high/xhigh/max, identical for v4-flash and v4-pro, per
# api-docs.deepseek.com/guides/thinking_mode) plus OR's "minimal" alias.
# "none" is handled by the OFF branch. Unknown values -> omit the effort key.
EFFORT_MAP = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "minimal": "low",
}


def _load_route_maps():
    """Builds {model_name: route} and {model_name: reasoning_dialect} from
    model_info.metadata of the model_list entries in config.yaml.

    - route (same source as time_router): surfaces as the metadata_route
      Prometheus label; the fallback taxonomy below keys on it.
    - reasoning_dialect: the entry's reasoning wire contract - "deepseek"
      (native thinking / reasoning_effort / reasoning_content) or
      "openrouter" (reasoning object). Declared per entry in config.yaml;
      the classifier prefers it over the route-label taxonomy.
    """
    for p in CONFIG_PATHS:
        if p and os.path.exists(p):
            with open(p) as f:
                cfg = yaml.safe_load(f)
            route_map = {}
            dialect_map = {}
            for m in (cfg.get("model_list") or []):
                name = m.get("model_name")
                if not name:
                    continue
                meta = (m.get("model_info") or {}).get("metadata") or {}
                route = meta.get("route")
                if route:
                    route_map[name] = route
                dialect = meta.get("reasoning_dialect")
                if dialect:
                    dialect_map[name] = dialect
            return route_map, dialect_map
    return {}, {}


ROUTE_MAP, DIALECT_MAP = _load_route_maps()


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


class ReasoningRouteAdapter(CustomLogger):
    """Normalizes reasoning control per the route bound in data["model"].

    The rules below are DESIGN.md sections 4.4 (OR-bound) and 4.5
    (native-bound), evaluated on the request dict in the documented order.
    """

    # ------------------------------------------------------------------ classification
    @classmethod
    def _classify(cls, model):
        """(side, label) for a model, or None when unclassified.

        Primary source: the entry-declared `reasoning_dialect` from
        config.yaml ("deepseek" -> native-bound rules, "openrouter" ->
        OR-bound rules). Fallback for entries without the declaration: the
        route-label taxonomy (NATIVE_ROUTE_LABELS / OR_ROUTE_LABELS). A
        declared-but-unrecognized dialect is fail-open: no-op.
        """
        if not isinstance(model, str):
            return None
        route = ROUTE_MAP.get(model)
        dialect = DIALECT_MAP.get(model)
        if dialect == "deepseek":
            return ("native", route or dialect)
        if dialect == "openrouter":
            return ("or", route or dialect)
        if dialect is not None:
            return None  # declared dialect outside the supported vocabulary
        if route in OR_ROUTE_LABELS:
            return ("or", route)
        if route in NATIVE_ROUTE_LABELS:
            return ("native", route)
        return None  # model unlisted or route label in neither set -> no-op

    # ------------------------------------------------------------------ OR-bound rules (DESIGN 4.4)
    @staticmethod
    def _normalize_or_bound(data: dict):
        """Make native-dialect control behave on OR. Returns an action name or None."""
        thinking = data.get("thinking")
        if not isinstance(thinking, dict):
            return None  # OR object / root effort / nothing -> leave as-is
        if thinking.get("type") == "disabled":
            # OR ignores `thinking` (row 1 of DESIGN 1.3); the OR contract OFF
            # object is its documented kill switch (row 2).
            data["reasoning"] = {"enabled": False, "effort": "none"}
            data.pop("thinking", None)
            data.pop("reasoning_effort", None)
            return "kill_switch_rescue"
        # enabled (or any other type value): OR ignores `thinking` and defaults
        # to reasoning ON; drop it. Never synthesize an object for ON (rows 3-4).
        data.pop("thinking", None)
        return "drop_thinking"

    # ------------------------------------------------------------------ native-bound rules (DESIGN 4.5)
    @staticmethod
    def _normalize_native(data: dict):
        """Translate an incoming OR `reasoning` object into native dialect."""
        reasoning = data.get("reasoning")
        if not isinstance(reasoning, dict):
            return None  # native dialect (thinking / root effort) -> pass through
        effort = reasoning.get("effort")
        if reasoning.get("enabled") is False or effort == "none":
            data["thinking"] = {"type": "disabled"}
            data.pop("reasoning", None)
            data.pop("reasoning_effort", None)
            return "object_to_native_off"
        if reasoning.get("enabled") is True:
            mapped = EFFORT_MAP.get(effort) if isinstance(effort, str) else None
            data["thinking"] = {"type": "enabled"}
            data.pop("reasoning", None)
            if mapped:
                data["reasoning_effort"] = mapped
            return "object_to_native_on"
        # enabled absent / not a bool -> outside the documented contract;
        # fail-open, leave unchanged.
        return None

    # ------------------------------------------------------------------ hook
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if os.environ.get("REASONING_ADAPTER_DISABLED"):
            return data
        try:
            model = data.get("model")
            classified = self._classify(model)
            debug = bool(os.environ.get("REASONING_ADAPTER_DEBUG"))
            if classified is None:
                if debug:
                    _log().info(
                        "ReasoningAdapter: model=%r unlisted (no route) -> no-op",
                        model,
                    )
                return data
            side, route = classified

            def _reasoning_keys():
                return {k: data.get(k) for k in ("thinking", "reasoning", "reasoning_effort")}

            before = _reasoning_keys() if debug else None
            normalize = self._normalize_or_bound if side == "or" else self._normalize_native
            action = normalize(data)
            if action is None:
                if debug:
                    _log().info(
                        "ReasoningAdapter: model=%s route=%s no-op (nothing to normalize)",
                        model,
                        route,
                    )
                return data
            if debug:
                _log().info(
                    "ReasoningAdapter: model=%s route=%s action=%s before=%s after=%s",
                    model,
                    route,
                    action,
                    _json.dumps(before, default=str),
                    _json.dumps(_reasoning_keys(), default=str),
                )
            else:
                _log().info(
                    "ReasoningAdapter: model=%s route=%s action=%s",
                    model,
                    route,
                    action,
                )
        except Exception as exc:  # fail-open: never break the request
            _rate_limited_warning(
                "ReasoningAdapter: normalization failed, request left unchanged: %r",
                exc,
            )
        return data


proxy_handler_instance = ReasoningRouteAdapter()
