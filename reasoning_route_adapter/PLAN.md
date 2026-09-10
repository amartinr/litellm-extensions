# PLAN — `reasoning_route_adapter`

Pending work, ordered by priority. From a code/documentation review; none of
these is implemented yet.

## P0 — Resolve the fallback re-execution question

Open question in `DESIGN.md` §7 / acceptance §6.2 item 5: whether
`async_pre_call_hook` re-runs on a `router_settings.fallbacks` attempt. If it
does not, a native-dialect payload (`thinking:disabled`) redirected to
`openrouter/...` is not rescued → silent reasoning over-spend on the path the
hook targets. Resolve with one live/DEBUG check; if confirmed, either
dual-spell OFF client-side or document the gap as accepted.

## P0 — Fail-open on config load

`_load_route_maps` parses `config.yaml` at import without `try/except`; a
malformed file raises at import. Wrap it (empty maps → the hook no-ops).

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
against `REASONING_ADAPTER_DISABLED=1`, fallback path.
