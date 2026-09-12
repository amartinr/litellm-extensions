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
    already-normalized payload is a no-op - relevant because the same kwargs is
    reused across attempts), fail-open (any error leaves the request
    unchanged, warned at most once per interval), stateless, no I/O.
    Rollback: REASONING_ADAPTER_DISABLED=1 (checked per request).
    Config is read through the shared `hook_config` loader: per-model facts
    from `model_info.metadata`, operation knobs from the top-level
    `callback_settings.reasoning_route_adapter` block (warning_interval_s,
    native_route_labels, or_route_labels).
    Env knobs: REASONING_ADAPTER_DEBUG (verbose logs incl. before/after
    reasoning keys), REASONING_ADAPTER_DISABLED.
required_litellm_version: 1.99.0
version: 0.3.0
licence: MIT
"""

import json as _json
import os
import time

import hook_config
from litellm.integrations.custom_logger import CustomLogger

# Populated by _load_config() at import and refreshed by tests/reloads.
ROUTE_MAP: dict[str, str] = {}
DIALECT_MAP: dict[str, str] = {}
NATIVE_ROUTE_LABELS: set = {"deepseek"}
OR_ROUTE_LABELS: set = {"baidu/fp8"}
_WARNING_INTERVAL_S = 300.0

# Fallback taxonomy - used only for deployments that do NOT declare
# model_info.metadata.reasoning_dialect (the declared dialect is the primary
# classification source). A route label outside both sets is unknown -> no-op.
# Defaults live in hook_config.SETTINGS_DEFAULTS; override via
# callback_settings.reasoning_route_adapter.

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
    global ROUTE_MAP, DIALECT_MAP, NATIVE_ROUTE_LABELS, OR_ROUTE_LABELS, _WARNING_INTERVAL_S
    ROUTE_MAP = {name: d["route"] for name, d in loaded.models.items() if d["route"]}
    DIALECT_MAP = {name: d["reasoning_dialect"] for name, d in loaded.models.items() if d["reasoning_dialect"]}
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


_last_warning_at = [0.0]


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
    both buckets are checked. Returns `{"dialect", "route", "name"}`.
    """
    dialect = route = name = None
    for bucket_name in ("metadata", "litellm_metadata"):
        bucket = kwargs.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        model_info = bucket.get("model_info")
        meta = model_info.get("metadata") if isinstance(model_info, dict) else None
        if isinstance(meta, dict):
            dialect = meta.get("reasoning_dialect", dialect)
            route = meta.get("route", route)
        candidate = bucket.get("deployment_model_name")
        if isinstance(candidate, str) and not name:
            name = candidate
    return {"dialect": dialect, "route": route, "name": name}


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
    def _normalize_or_bound(kwargs: dict):
        """Make native-dialect control behave on OR. Returns an action name or None."""
        thinking = kwargs.get("thinking")
        if not isinstance(thinking, dict):
            return None  # OR object / root effort / nothing -> leave as-is
        if thinking.get("type") == "disabled":
            # OR ignores `thinking` (row 1 of DESIGN 1.3); the OR contract OFF
            # object is its documented kill switch (row 2).
            kwargs["reasoning"] = {"enabled": False, "effort": "none"}
            kwargs.pop("thinking", None)
            kwargs.pop("reasoning_effort", None)
            return "kill_switch_rescue"
        # enabled (or any other type value): OR ignores `thinking` and defaults
        # to reasoning ON; drop it. Never synthesize an object for ON (rows 3-4).
        kwargs.pop("thinking", None)
        return "drop_thinking"

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
            normalize = self._normalize_or_bound if side == "or" else self._normalize_native
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
