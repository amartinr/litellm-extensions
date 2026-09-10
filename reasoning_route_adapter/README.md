# `reasoning_route_adapter`

LiteLLM pre-call hook (`CustomLogger`), registered AFTER `time_router` so
`data["model"]` is the post-reroute model. Registered as
`reasoning_route_adapter.proxy_handler_instance`.

Normalizes reasoning control to the dialect of the bound route. Never touches
`messages`. Deterministic, idempotent, fail-open, stateless, no per-request
I/O.

## Why

Clients (Open WebUI, pi) send the DeepSeek-native dialect on requests that
carry reasoning control (root `thinking`/`reasoning_effort`, `reasoning_content`
on assistant messages). On the OpenRouter route, OR ignores `thinking`
(`thinking:{type:"disabled"}` still reasons and bills); it honors its own
`reasoning` object and the root `reasoning_effort` (effort level only). The
adapter rescues the kill switch on OR and translates an incoming OR object on
the native route.

## Rules

Classification (`_classify`), two layers:

1. declared dialect (`model_info.metadata.reasoning_dialect`):
   `"deepseek"` → native-bound, `"openrouter"` → OR-bound; any other declared
   value → no-op;
2. fallback route-label sets (`REASONING_ADAPTER_NATIVE_ROUTES` default
   `deepseek`, `REASONING_ADAPTER_OR_ROUTES` default `baidu/fp8`);
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

The native dialect (`thinking` / root `reasoning_effort`) passes through
untouched. Rollback: `REASONING_ADAPTER_DISABLED=1`.

## Configuration

Requires `model_info.metadata.reasoning_dialect` on reasoning-capable
`model_list` entries (primary classification); the route-label sets are only
the fallback. See [`../config.yaml.example`](../config.yaml.example).

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `REASONING_ADAPTER_DISABLED` | unset | `1` disables normalization (rollback) |
| `REASONING_ADAPTER_DEBUG` | unset | Verbose logging incl. before/after reasoning keys |
| `REASONING_ADAPTER_NATIVE_ROUTES` | `deepseek` | Native-bound route labels (fallback only) |
| `REASONING_ADAPTER_OR_ROUTES` | `baidu/fp8` | OR-bound route labels (fallback only) |
| `LITELLM_CONFIG_FILE` | unset | Config path override (default: `/app/config.yaml`, then `./config.yaml`) |

## Logging

One line per applied transformation:
`ReasoningAdapter: model=... route=... action=...`.
`REASONING_ADAPTER_DEBUG=1` adds `before=`/`after=`.

## Deployment

Mounted at `/app/reasoning_route_adapter.py` next to `config.yaml` and
`time_router.py`.

## Tests

```
.venv/bin/python reasoning_route_adapter/tests/test_reasoning_route_adapter.py
```

Offline acceptance checks for `DESIGN.md` §6.1: stdlib only, no network
(`yaml` and `litellm` stubbed). 16/16.

## Probes

Live probes and recorded results: [`probes/README.md`](probes/README.md).

## Design

Rules, measured evidence, acceptance criteria and open questions:
[`DESIGN.md`](DESIGN.md).

## Pending work

See [`PLAN.md`](PLAN.md).
