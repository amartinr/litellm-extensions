# `reasoning_route_adapter`

LiteLLM per-deployment hook (`CustomLogger`), registered as
`reasoning_route_adapter.proxy_handler_instance`. Runs on
`async_pre_call_deployment_hook`, i.e. once per real deployment attempt
(original, retry, fallback step), after the router has selected the
deployment and before the request is sent. It classifies by the **bound
deployment**, so it no longer depends on registration order.

Normalizes reasoning control to the dialect of the bound route. Never touches
`messages`. Deterministic, idempotent (the same kwargs is reused across
attempts), fail-open, stateless, no per-request I/O.

## Why

Clients (Open WebUI, pi) send the DeepSeek-native dialect on requests that
carry reasoning control (root `thinking`/`reasoning_effort`, `reasoning_content`
on assistant messages). On the OpenRouter route, OR ignores `thinking`
(`thinking:{type:"disabled"}` still reasons and bills); it honors its own
`reasoning` object and the root `reasoning_effort` (effort level only). The
adapter rescues the kill switch on OR and translates an incoming OR object on
the native route. Because it runs per attempt, the rescue also covers a
`router_settings.fallbacks` redirect to OR.

## Rules

Classification (`_classify`), on the bound deployment:

1. declared dialect (`model_info.metadata.reasoning_dialect`, read from the
   deployment metadata): `"deepseek"` → native-bound, `"openrouter"` →
   OR-bound; any other declared value → no-op;
2. fallback, when the declaration is absent: `deployment_model_name` →
   `DIALECT_MAP`/`ROUTE_MAP`, then the route-label sets from
   `callback_settings.reasoning_route_adapter` (`native_route_labels` default
   `["deepseek"]`, `or_route_labels` default `["baidu/fp8"]`);
3. neither → no-op.

OR-bound (route label `baidu/fp8`):

| Input | Output | Action |
|---|---|---|
| `thinking.type == "disabled"` | `reasoning = {enabled:false, effort:"none"}`; drop `thinking` and `reasoning_effort` | `kill_switch_rescue` |
| any other `thinking.type` | drop `thinking`; no synthesized object | `drop_thinking` |
| no `thinking` | `reasoning` / `reasoning_effort` untouched | — |

Native-bound (route label `deepseek`); only acts on an incoming OR object:

| Input | Output | Action |
|---|---|---|
| `enabled is False` or `effort == "none"` | `thinking = {type:"disabled"}`; drop `reasoning` and `reasoning_effort` | `object_to_native_off` |
| `enabled is True` | `thinking = {type:"enabled"}` + mapped `reasoning_effort`; drop `reasoning` | `object_to_native_on` |
| `enabled` absent / not bool | unchanged | — |

`EFFORT_MAP`: `low→low`, `medium→high`, `high→high`, `xhigh→high`, `max→max`,
`minimal→low`. Unmappable → `thinking` enabled without `reasoning_effort`.
This is a provider protocol fact (probe-backed); it stays in code, not config.

The native dialect (`thinking` / root `reasoning_effort`) passes through
untouched. Rollback: `REASONING_ADAPTER_DISABLED=1`.

## Configuration

Read through the shared [`hook_config`](../hook_config.py) loader:

- **Per-model facts** — `model_info.metadata.reasoning_dialect` (primary
  classification) and `route`; the route-label sets are only the fallback.
- **Operation knobs** — top-level `callback_settings.reasoning_route_adapter`:
  `warning_interval_s` (default 300), `native_route_labels`, `or_route_labels`.

See [`../config.yaml.example`](../config.yaml.example).

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `REASONING_ADAPTER_DISABLED` | unset | `1` disables normalization (rollback) |
| `REASONING_ADAPTER_DEBUG` | unset | Verbose logging incl. before/after reasoning keys |
| `LITELLM_CONFIG_FILE` | unset | Config path override (default: `/app/config.yaml`, then `./config.yaml`) |

The route-label sets and `warning_interval_s` live in `config.yaml`
(`callback_settings.reasoning_route_adapter`), not in the environment.

## Logging

One line per applied transformation:
`ReasoningAdapter: deployment=... route=... action=...`.
`REASONING_ADAPTER_DEBUG=1` adds `before=`/`after=`.

## Deployment

Mounted at `/app/reasoning_route_adapter.py` next to `config.yaml`,
`hook_config.py` and `time_router.py`.

## Tests

```
.venv-test/bin/python -m pytest reasoning_route_adapter/tests/
```

Offline pytest: classification by the bound deployment, OR/native rules,
idempotency, the fallback-attempt re-normalization, both metadata buckets,
rollback env, fail-open and config wiring.

## Probes

Live probes and recorded results: [`probes/README.md`](probes/README.md).

## Design

Rules, measured evidence, acceptance criteria and open questions:
[`DESIGN.md`](DESIGN.md).

## Pending work

See [`PLAN.md`](PLAN.md).
