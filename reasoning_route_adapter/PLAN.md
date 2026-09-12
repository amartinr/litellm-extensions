# PLAN — `reasoning_route_adapter`

Remaining work. The previous revision's items are implemented: the migration to
`async_pre_call_deployment_hook` (per-attempt, fallback coverage), classification
by the bound deployment, the shared `hook_config` loader (per-model descriptors
+ `callback_settings.reasoning_route_adapter` knobs), fail-open, and the pytest
suite with real asserts. See `DESIGN.md` §4–§7 and the root README.

## Decision — Root `reasoning_effort` is the client's responsibility

The adapter normalizes the dialect of the bound route; it does not keep a
second, per-target vocabulary for root `reasoning_effort`. Out-of-vocabulary
root values (`"medium"`, `"xhigh"`, `"minimal"`, `"none"`) pass through
unchanged, and the client must send a value the bound route honors (pi does
this via its `thinkingLevelMap`). Do not add per-route root normalization.

## Decision — No custom metrics

Use LiteLLM's native metrics only; no custom Prometheus counters are added.
The adapter's actions stay in the logs (one line per transformation), which is
enough for debugging. (Counters for `kill_switch_rescue` / `drop_thinking` /
`object_to_native_*` were considered to catch regressions; dropped.)

## P2 — Malformed `thinking` handling

`_normalize_or_bound` drops `thinking` for any type other than `"disabled"`,
including malformed values (`{"type":"foo"}`). Decide whether to no-op on
unrecognized types instead of silently dropping.

## P2 — Live acceptance §6.2

Normal path verified (probe 4, `probes/gateway_contract.py`, 4/4): OR +
`thinking:disabled` → 0 tokens (the deployed adapter rescues via the bound
deployment), native + `thinking:disabled` → 0 tokens, defaults reason.

Still to run: native OR-object OFF → 0 tokens; native OR-object ON →
low-range; regression against `REASONING_ADAPTER_DISABLED=1`; and the fallback
path (native failure → OR), which needs
`general_settings.dangerously_allow_mock_testing_request_params` or a broken
primary (see `probes/README.md` probe 4).
