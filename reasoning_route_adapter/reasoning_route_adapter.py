"""
title: LiteLLM Reasoning Route Adapter (per-route reasoning-dialect normalization)
id: reasoning_route_adapter
author: A. Martin
author_url: https://github.com/amartinr
description: >
    LiteLLM custom per-deployment hook (CustomLogger). Runs on
    `async_pre_call_deployment_hook`, i.e. once per real deployment attempt
    (original, retry, fallback step), after the router has selected the
    deployment and before the request is sent. It normalizes the reasoning
    control of the request to the dialect that the bound route honors.
    Route classification is by the bound deployment: the deployment's
    `model_info.metadata.reasoning_dialect` ("deepseek" -> native-bound,
    "openrouter" -> OR-bound); when absent, the `deployment_model_name` is
    looked up in the config maps, then the route-label taxonomy.
    Clients (Open WebUI and pi, each with an extension) send the
    DeepSeek-native dialect - root keys `thinking:{type:enabled|disabled}` and
    `reasoning_effort` (the raw HTTP contract of api.deepseek.com, see its
    curl example - the OpenAI-SDK `extra_body` wrapper is an SDK artifact, not
    the wire format LiteLLM forwards). Evidence: probes/, 2026-09-08; DESIGN.md.

    OR-bound (route label "baidu/fp8" / "deepinfra/fp8", OpenRouter):
      - `thinking:{type:"disabled"}` is IGNORED by OR (probed: still reasoned,
        47-57 tokens) -> rescued into the OR contract OFF
        `reasoning:{enabled:false, effort:"none"}` (0 tokens); `thinking` and
        any root `reasoning_effort` are dropped.
      - `thinking:{type:"enabled"}` is ignored too -> dropped.
      - root `reasoning_effort` is translated into the OR object
        `reasoning:{enabled:true, effort:<mapped>}` only when the bound
        deployment declares
        `model_info.metadata.reasoning_effort_requires_object: true`
        (LiteLLM's openrouter map lags some new model ids, e.g.
        deepseek-v4.1-flash-1017 400s on raw root effort 2026-10-20, while the
        object spelling passes; live: low 41 / high 71 / max 155 reasoning
        tokens on deepinfra/fp8). `none` maps to the OFF object.
        Unknown/non-string levels are dropped fail-safe (reasoning stays
        ON/default) to avoid the 400. When the flag is absent or false, the
        root effort passes through unchanged (legacy behavior).
      - an incoming OR `reasoning` object passes through; a co-present root
        `reasoning_effort` is dropped only for deployments that require the
        object (the object is canonical and the root would 400).
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
    already-normalized payload is a no-op - relevant because the same kwargs is
    reused across attempts), fail-open (any error leaves the request
    unchanged, warned at most once per interval), stateless, no I/O.
    Rollback: REASONING_ADAPTER_DISABLED=1 (checked per request).
    Config is read through the shared `hook_config` loader: per-model facts
    from `model_info.metadata` (`route`, `reasoning_dialect`,
    `reasoning_effort_requires_object`), operation knobs from the top-level
    `callback_settings.reasoning_route_adapter` block (warning_interval_s,
    native_route_labels, or_route_labels).
    Env knobs: REASONING_ADAPTER_DEBUG (verbose logs incl. before/after
    reasoning keys), REASONING_ADAPTER_DISABLED.
required_litellm_version: 1.99.0
version: 0.4.0
licence: MIT
"""

import json as _json
import os
import sys
import time

# See time_router: load hook_config from this file's directory, not the cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hook_config
from litellm.integrations.custom_logger import CustomLogger

# Populated by _load_config() at import and refreshed by tests/reloads.
ROUTE_MAP: dict[str, str] = {}
DIALECT_MAP: dict[str, str] = {}
EFFORT_REQUIRES_OBJECT: dict[str, bool] = {}
NATIVE_ROUTE_LABELS: set = {"deepseek"}
OR_ROUTE_LABELS: set = {"baidu/fp8"}
_NEVER_WARNED_AT = 0.0
_WARNING_INTERVAL_S = hook_config.DEFAULT_WARNING_INTERVAL_S

# Fallback taxonomy - used only for deployments that do NOT declare
# model_info.metadata.reasoning_dialect (the declared dialect is the primary
# classification source). A route label outside both sets is unknown -> no-op.
# Defaults live in hook_config.SETTINGS_DEFAULTS; override via
# callback_settings.reasoning_route_adapter.

# Root effort values that OpenRouter honors on DeepSeek models that opt into
# object effort (GET /models 2026-10-20 for deepseek-v4.1-flash-1017:
# supported_efforts ["max", "high", "low", "none"], default "high").
# Kept in code: provider protocol fact, not operator-tunable.
# "medium"/"xhigh"/"minimal" aliases collapse through EFFORT_MAP so OWUI's
# Medium button ("medium") still grades instead of 400ing.
# "none" is handled by the OFF branch. Unknown values -> dropped fail-safe.
OR_OBJECT_EFFORTS = frozenset({"low", "high", "max"})


# OR-object effort -> native root `reasoning_effort`. DeepSeek's own collapse
# table (low/medium/high/xhigh/max, identical for v4-flash and v4-pro, per
# api-docs.deepseek.com/guides/thinking_mode) plus OR's "minimal" alias.
# "none" is handled by the OFF branch. Unknown values -> omit the effort key.
# Provider protocol fact, probe-backed: keep in code, do not move to config.
EFFORT_MAP = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "minimal": "low",
}


def _apply_config(loaded) -> None:
    global ROUTE_MAP, DIALECT_MAP, EFFORT_REQUIRES_OBJECT
    global NATIVE_ROUTE_LABELS, OR_ROUTE_LABELS, _WARNING_INTERVAL_S
    ROUTE_MAP = {name: d["route"] for name, d in loaded.models.items() if d["route"]}
    DIALECT_MAP = {name: d["reasoning_dialect"] for name, d in loaded.models.items() if d["reasoning_dialect"]}
    EFFORT_REQUIRES_OBJECT = {
        name: d["reasoning_effort_requires_object"]
        for name, d in loaded.models.items()
        if d.get("reasoning_effort_requires_object")
    }
    settings = loaded.settings("reasoning_route_adapter")
    NATIVE_ROUTE_LABELS = set(settings["native_route_labels"])
    OR_ROUTE_LABELS = set(settings["or_route_labels"])
    _WARNING_INTERVAL_S = settings["warning_interval_s"]


def _load_config(path: str | None = None) -> None:
    _apply_config(hook_config.load(path))


_load_config()


def _log():
    """LiteLLM's proxy logger - shared loader handles the lazy import."""
    return hook_config.get_logger()


_last_warning_at = [_NEVER_WARNED_AT]


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


def deployment_context(kwargs: dict) -> dict:
    """Bound-deployment facts from the router metadata bucket.

    LiteLLM exposes `model_info` and `deployment_model_name` under `metadata`
    for most call types (and `litellm_metadata` for a few router methods), so
    both buckets are checked. Returns `{"dialect", "route", "name",
    "requires_object"}`.
    """
    dialect = route = name = None
    requires_object = None
    for bucket_name in ("metadata", "litellm_metadata"):
        bucket = kwargs.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        model_info = bucket.get("model_info")
        meta = model_info.get("metadata") if isinstance(model_info, dict) else None
        if isinstance(meta, dict):
            dialect = meta.get("reasoning_dialect", dialect)
            route = meta.get("route", route)
            declared = meta.get("reasoning_effort_requires_object")
            if isinstance(declared, bool):
                requires_object = declared
        candidate = bucket.get("deployment_model_name")
        if isinstance(candidate, str) and not name:
            name = candidate
    if requires_object is None and isinstance(name, str):
        requires_object = EFFORT_REQUIRES_OBJECT.get(name, False)
    return {
        "dialect": dialect,
        "route": route,
        "name": name,
        "requires_object": bool(requires_object),
    }


class ReasoningRouteAdapter(CustomLogger):
    """Normalizes reasoning control per the bound deployment.

    The rules are DESIGN.md sections 4.4 (OR-bound) and 4.5 (native-bound),
    evaluated on the request kwargs in the documented order.
    """

    # ------------------------------------------------------------------ classification
    @classmethod
    def _classify(cls, context: dict):
        """(side, label) for a bound deployment, or None when unclassified.

        Primary source: the deployment's
        `model_info.metadata.reasoning_dialect` ("deepseek" -> native-bound
        rules, "openrouter" -> OR-bound rules). Fallback for deployments
        without the declaration: `deployment_model_name` against
        `DIALECT_MAP` / `ROUTE_MAP`, then the route-label taxonomy. A
        declared-but-unrecognized dialect is fail-open: no-op.
        """
        dialect = context.get("dialect")
        route = context.get("route")
        name = context.get("name")
        label = route if isinstance(route, str) and route else name

        if dialect == "deepseek":
            return ("native", label or dialect)
        if dialect == "openrouter":
            return ("or", label or dialect)
        if dialect is not None:
            return None  # declared dialect outside the supported vocabulary

        if not isinstance(name, str):
            return None
        declared = DIALECT_MAP.get(name)
        if declared == "deepseek":
            return ("native", ROUTE_MAP.get(name) or declared)
        if declared == "openrouter":
            return ("or", ROUTE_MAP.get(name) or declared)
        if declared is not None:
            return None
        label = ROUTE_MAP.get(name)
        if label in OR_ROUTE_LABELS:
            return ("or", label)
        if label in NATIVE_ROUTE_LABELS:
            return ("native", label)
        return None  # unlisted deployment or route label in neither set -> no-op

    # ------------------------------------------------------------------ OR-bound rules (DESIGN 4.4)
    @staticmethod
    def _normalize_or_bound_root(kwargs: dict):
        """Legacy OR behavior: only drop `thinking` and rescue the kill switch."""
        thinking = kwargs.get("thinking")
        if not isinstance(thinking, dict):
            return None  # OR object / root effort / nothing -> leave as-is
        if thinking.get("type") == "disabled":
            kwargs["reasoning"] = {"enabled": False, "effort": "none"}
            kwargs.pop("thinking", None)
            kwargs.pop("reasoning_effort", None)
            return "kill_switch_rescue"
        kwargs.pop("thinking", None)
        return "drop_thinking"

    @staticmethod
    def _is_reasoning_object(value) -> bool:
        """True for a well-formed OR reasoning object (at least one known key)."""
        return isinstance(value, dict) and (
            isinstance(value.get("enabled"), bool)
            or isinstance(value.get("effort"), str)
        )

    @classmethod
    def _normalize_or_bound_object(cls, kwargs: dict):
        """OR behavior for deployments that require the reasoning object."""
        thinking = kwargs.get("thinking")
        reasoning = kwargs.get("reasoning")
        effort = kwargs.get("reasoning_effort")

        # Native kill switch first: OR ignores `thinking`; the OR contract OFF
        # object is its documented kill switch.
        if isinstance(thinking, dict) and thinking.get("type") == "disabled":
            kwargs["reasoning"] = {"enabled": False, "effort": "none"}
            kwargs.pop("thinking", None)
            kwargs.pop("reasoning_effort", None)
            return "kill_switch_rescue"

        # Fail-open on malformed values instead of building a hybrid payload.
        if "thinking" in kwargs and thinking is not None and not isinstance(thinking, dict):
            return None
        if "reasoning" in kwargs and reasoning is not None and not isinstance(reasoning, dict):
            return None
        if isinstance(reasoning, dict) and not cls._is_reasoning_object(reasoning):
            return None

        action = None
        if isinstance(thinking, dict):
            kwargs.pop("thinking", None)
            action = "drop_thinking"

        # An incoming OR object is canonical: drop a co-present root effort.
        if isinstance(reasoning, dict):
            if "reasoning_effort" in kwargs:
                kwargs.pop("reasoning_effort", None)
                return "drop_root_effort_for_object"
            return action

        # No object: translate root effort into the OR object spelling.
        if isinstance(effort, str):
            if effort == "none":
                kwargs["reasoning"] = {"enabled": False, "effort": "none"}
                kwargs.pop("reasoning_effort", None)
                return "root_effort_to_object_off"
            mapped = EFFORT_MAP.get(effort, effort)
            if mapped in OR_OBJECT_EFFORTS:
                kwargs["reasoning"] = {"enabled": True, "effort": mapped}
                kwargs.pop("reasoning_effort", None)
                return "root_effort_to_object"
            # Unknown level: drop fail-safe rather than 400 the whole request.
            kwargs.pop("reasoning_effort", None)
            return action or "drop_unknown_effort"

        if "reasoning_effort" in kwargs:
            # Present but non-string (e.g. 5, None): drop fail-safe.
            kwargs.pop("reasoning_effort", None)
            return action or "drop_unknown_effort"

        return action

    # ------------------------------------------------------------------ native-bound rules (DESIGN 4.5)
    @staticmethod
    def _normalize_native(kwargs: dict):
        """Translate an incoming OR `reasoning` object into native dialect."""
        reasoning = kwargs.get("reasoning")
        if not isinstance(reasoning, dict):
            return None  # native dialect (thinking / root effort) -> pass through
        effort = reasoning.get("effort")
        if reasoning.get("enabled") is False or effort == "none":
            kwargs["thinking"] = {"type": "disabled"}
            kwargs.pop("reasoning", None)
            kwargs.pop("reasoning_effort", None)
            return "object_to_native_off"
        if reasoning.get("enabled") is True:
            mapped = EFFORT_MAP.get(effort) if isinstance(effort, str) else None
            kwargs["thinking"] = {"type": "enabled"}
            kwargs.pop("reasoning", None)
            if mapped:
                kwargs["reasoning_effort"] = mapped
            return "object_to_native_on"
        # enabled absent / not a bool -> outside the documented contract;
        # fail-open, leave unchanged.
        return None

    # ------------------------------------------------------------------ hook
    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type):
        """Runs per real deployment attempt; returns kwargs (or None-unchanged)."""
        if os.environ.get("REASONING_ADAPTER_DISABLED"):
            return kwargs
        if not isinstance(kwargs, dict):
            return kwargs
        try:
            context = deployment_context(kwargs)
            classified = self._classify(context)
            debug = bool(os.environ.get("REASONING_ADAPTER_DEBUG"))
            if classified is None:
                if debug:
                    _log().info(
                        "ReasoningAdapter: deployment=%r unclassified -> no-op",
                        context.get("name"),
                    )
                return kwargs
            side, route = classified

            def _reasoning_keys():
                return {k: kwargs.get(k) for k in ("thinking", "reasoning", "reasoning_effort")}

            before = _reasoning_keys() if debug else None
            if side == "native":
                normalize = self._normalize_native
            elif context.get("requires_object"):
                normalize = self._normalize_or_bound_object
            else:
                normalize = self._normalize_or_bound_root
            action = normalize(kwargs)
            if action is None:
                if debug:
                    _log().info(
                        "ReasoningAdapter: deployment=%s route=%s no-op (nothing to normalize)",
                        context.get("name"),
                        route,
                    )
                return kwargs
            if debug:
                _log().info(
                    "ReasoningAdapter: deployment=%s route=%s action=%s before=%s after=%s",
                    context.get("name"),
                    route,
                    action,
                    _json.dumps(before, default=str),
                    _json.dumps(_reasoning_keys(), default=str),
                )
            else:
                _log().info(
                    "ReasoningAdapter: deployment=%s route=%s action=%s",
                    context.get("name"),
                    route,
                    action,
                )
        except Exception as exc:  # fail-open: never break the request
            _rate_limited_warning(
                "ReasoningAdapter: normalization failed, request left unchanged: %r",
                exc,
            )
        return kwargs


proxy_handler_instance = ReasoningRouteAdapter()
