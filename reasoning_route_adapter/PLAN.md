# PLAN — `reasoning_route_adapter`

Remaining work. The previous revision's items are implemented: the migration to
`async_pre_call_deployment_hook` (per-attempt, fallback coverage), classification
by the bound deployment, the shared `hook_config` loader (per-model descriptors
+ `callback_settings.reasoning_route_adapter` knobs), fail-open, and the pytest
suite with real asserts. See `DESIGN.md` §4–§7 and the root README.

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

Normal path verified (probe 4, `probes/gateway_contract.py`, 4/4): OR +
`thinking:disabled` → 0 tokens (the deployed adapter rescues via the bound
deployment), native + `thinking:disabled` → 0 tokens, defaults reason.

Still to run: native OR-object OFF → 0 tokens; native OR-object ON →
low-range; regression against `REASONING_ADAPTER_DISABLED=1`; and the fallback
path (native failure → OR), which needs
`general_settings.dangerously_allow_mock_testing_request_params` or a broken
primary (see `probes/README.md` probe 4).
