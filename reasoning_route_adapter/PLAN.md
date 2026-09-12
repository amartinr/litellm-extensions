# PLAN — `reasoning_route_adapter`

Pending work, ordered by priority. From a code/documentation review; none of
these is implemented yet.

## Configuration target (reference for the items below)

Two surfaces, corrected against LiteLLM v1.99.0:

- **Facts** stay where they are: `model_info.metadata.route` and
  `model_info.metadata.reasoning_dialect`.
- **Operation knobs** → `callback_settings.reasoning_route_adapter` at the
  **top level** of `config.yaml`. v1.99.0 reads
  `config.get("callback_settings")` in `proxy_server.py` and exposes it as
  `litellm.callback_settings`; it is NOT `litellm_settings.callback_settings`.
  A `CustomLogger` registered by dotted path does **not** receive its block
  automatically (only built-in callbacks get `callback_specific_params` via
  `initialize_callbacks_on_proxy`), so the hook reads it itself via
  `hook_config.settings("reasoning_route_adapter")`.

```yaml
callback_settings:                   # top level, NOT under litellm_settings
  reasoning_route_adapter:
    warning_interval_s: 300
    native_route_labels: ["deepseek"]
    or_route_labels: ["baidu/fp8"]
```

Stays in code (do **not** move to config): `EFFORT_MAP` and the synthetic OFF
object `{"enabled": false, "effort": "none"}`. They are provider protocol
facts backed by `probes/` (DeepSeek's collapse table; OpenRouter's
kill-switch contract), not operator-tunable knobs; a config surface could
diverge from the provider's actual semantics.

Config is read through the shared `hook_config` module (`load()` for the
`Loaded.models` descriptors, `.settings()` for the knobs above) — see
`time_router/PLAN.md` and `DESIGN.md` §5.2.

The adapter overrides `async_pre_call_deployment_hook` (runs per real
deployment attempt), **not** `async_pre_call_hook` — see P0 below and
`DESIGN.md` §4.1/§7.

## P0 — Migrate to `async_pre_call_deployment_hook` (fallback gap)

Decided (`DESIGN.md` §7): the request-level `async_pre_call_hook` runs once,
before routing, and does not re-run on a `router_settings.fallbacks` step, so
a native-dialect payload (`thinking:disabled`) redirected to `openrouter/...`
is not rescued → silent reasoning over-spend for the whole native
outage/cooldown window. `async_pre_call_deployment_hook` runs per real
deployment attempt (original, retry, fallback step) and sees the bound
deployment, so the adapter moves there.

Work:
- change the override to `async_pre_call_deployment_hook(self, kwargs,
  call_type) -> dict | None`; operate on `kwargs` root keys and return
  `kwargs`.
- classify by the bound deployment (`DESIGN.md` §4.3): prefer
  `kwargs[metadata]["model_info"]["metadata"]["reasoning_dialect"]`, fall
  back to `deployment_model_name` → `DIALECT_MAP`/`ROUTE_MAP`.
- read both metadata buckets (`metadata`, `litellm_metadata`).
- keep the normalization rules (`_normalize_or_bound` / `_normalize_native`)
  and the `REASONING_ADAPTER_DISABLED` rollback untouched.
- update `config.yaml.example` / README: the "time_router first, adapter
  reads the rerouted model" ordering note no longer applies.
- confirm once in DEBUG that the hook fires on a chat-completions fallback.

## P0 — Fail-open on config load

Owned by the shared loader (see "Configuration target"): unreadable / invalid
YAML → no models, the hook no-ops, one ERROR log; invalid
`callback_settings` types → documented default + ERROR. The hook keeps its
`try/except` around the deployment-hook body (fail-open).

## P1 — Move operation knobs to `callback_settings`

Replace the `os.environ.get(...)` defaults in `_label_set`
(`REASONING_ADAPTER_NATIVE_ROUTES`, `REASONING_ADAPTER_OR_ROUTES`) and
`_WARNING_INTERVAL_S` with `hook_config.settings("reasoning_route_adapter")`
(`native_route_labels`, `or_route_labels`, `warning_interval_s`). Document
the defaults. Update `reasoning_route_adapter/README.md` and `.env.example`
(remove the moved env vars).

## P1 — Root `reasoning_effort` passthrough

The adapter only normalizes the incoming OR object. Out-of-vocabulary root
values (`"medium"`, `"xhigh"`, `"minimal"`, `"none"`) pass through unchanged on
both routes. Safe for pi today (its `thinkingLevelMap` filters unsupported
levels), fragile for any client that does not. Decide: normalize the root value
against the route vocabulary, or document the client obligation.

## P2 — Observability

Only log lines today. Counters for `kill_switch_rescue` / `drop_thinking` /
`object_to_native_*` would catch regressions. Constrained by LiteLLM #38660
(fallback/failure emitters do not populate `custom_prometheus_metadata_labels`);
scope to success-path metrics first.

## P2 — Malformed `thinking` handling

`_normalize_or_bound` drops `thinking` for any type other than `"disabled"`,
including malformed values (`{"type":"foo"}`). Decide whether to no-op on
unrecognized types instead of silently dropping.

## P2 — Live acceptance §6.2

Run the gateway checks in `DESIGN.md` §6.2 after deployment: OR kill switch → 0
tokens, native object OFF → 0 tokens, native object ON → low-range, regression
against `REASONING_ADAPTER_DISABLED=1`, and the fallback path (the
per-deployment hook must rescue `thinking:disabled` when the router falls back
to OR).
