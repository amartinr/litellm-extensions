# PLAN — `reasoning_route_adapter`

Remaining work. The previous revision's items are implemented: the migration to
`async_pre_call_deployment_hook` (per-attempt, fallback coverage), classification
by the bound deployment, the shared `hook_config` loader (per-model descriptors
+ `callback_settings.reasoning_route_adapter` knobs), fail-open, and the pytest
suite with real asserts. See `DESIGN.md` §4–§7 and the root README.

## Decision — Root effort translation is per-deployment opt-in

The adapter normalizes the dialect of the bound route. Translating root
`reasoning_effort` into the OR object is a workaround for OpenRouter model ids
whose LiteLLM provider map lags and rejects the raw root key. It is therefore
opt-in per deployment via:

```yaml
model_info:
  metadata:
    reasoning_effort_requires_object: true
```

When the flag is absent or false, root `reasoning_effort` passes through
unchanged (legacy behavior), out-of-vocabulary values included. Do not apply
the object spelling globally: other OpenRouter models may still accept the
root key, and their effort vocabulary is not necessarily `low/high/max`.

## Decision — No custom metrics

Use LiteLLM's native metrics only; no custom Prometheus counters are added.
The adapter's actions stay in the logs (one line per transformation), which is
enough for debugging. (Counters for `kill_switch_rescue` / `drop_thinking` /
`object_to_native_*` were considered to catch regressions; dropped.)

## Finding — Effort *level* is not usable through this gateway (LiteLLM v1.99.0)

Measured 2026-09-12 after the `openrouter/` prefix. Only the ON/OFF kill
switch is reliable; the effort **level** is not. Full data and sources in
`DESIGN.md` §1.4.

- **Native** (`deepseek/...`): LiteLLM's `DeepSeekChatConfig` discards
  `reasoning_effort` ([litellm #27439](https://github.com/BerriAI/litellm/issues/27439));
  every non-`none` level = default (`high`). The adapter's `EFFORT_MAP`
  output is therefore inert on native — only its `thinking` part acts.
- **OR** (`openrouter/...`): at measurement time the provider
  (`streamlake/fp8`) honoured the level erratically (inverted / no ordering,
  σ 200–455). Reproduced via the OR object, so it is provider-side, not the
  LiteLLM path. The current deployment uses `deepinfra/fp8`; the 2026-10-20
  live measurements there are ordered (low 41 / high 71 / max 155 reasoning
  tokens), so the object-effort opt-in is enabled for
  `deepseek-v4.1-flash-1017`. Do not generalize those numbers to other
  pinned providers.
- **Follow-up (P2)**: track [litellm PR #40717](https://github.com/BerriAI/litellm/pull/40717)
  (open, 2026-09-11), which forwards `reasoning_effort` on the native path
  but **only when no explicit `thinking` toggle is present**. pi's `compat`
  delta (`DEPLOYMENT.md` §1) and the adapter's `object_to_native_on` send
  `thinking:{type:"enabled"}` *plus* `reasoning_effort`, so native would
  remain inert even after the PR.

  Recorded activation path (forward-compatible; do **not** implement until
  the PR merges or until the decision to activate is taken — today it would
  not change behaviour):

  1. `_normalize_native`: drop `thinking:{type:"enabled"}` when
     `reasoning_effort` is present (LiteLLM re-adds the toggle and, post-PR,
     forwards the level);
  2. `object_to_native_on`: set `reasoning_effort` only, no explicit
     `thinking`;
  3. tests for both; then re-check `DESIGN` §4.4 rule 3 ("cheap on OR" no
     longer a safe assumption) and `EFFORT_MAP` end-to-end.

  The OR route needs nothing for the default path: ON already sends root
  `reasoning_effort` only (the adapter drops `thinking`), and the
  provider-side erratic behaviour is out of LiteLLM's hands (§1.4).
  Deployments that opt into `reasoning_effort_requires_object` use the object
  spelling instead; that path is tracked by the live checks in §6.2.
- **Do not** build cost logic on the level: rely on the kill switch
  (0 tokens) and `max_tokens`.

## P2 — Malformed `thinking` handling

The default OR path drops `thinking` for any type other than `"disabled"`,
including malformed values (`{"type":"foo"}`). The object-effort path is
fail-open on malformed `thinking`/`reasoning` values. Decide whether the
default path should also no-op on unrecognized types instead of silently
dropping them.

## P2 — Live acceptance §6.2

Normal path verified (probe 4, `probes/gateway_contract.py`, 4/4): OR +
`thinking:disabled` → 0 tokens (the deployed adapter rescues via the bound
deployment), native + `thinking:disabled` → 0 tokens, defaults reason.

Still to run: native OR-object OFF → 0 tokens; native OR-object ON →
low-range; regression against `REASONING_ADAPTER_DISABLED=1`; the object-effort
opt-in on `deepinfra/fp8` (root effort → 200, expected token range); and the
fallback path (native failure → OR), which needs
`general_settings.dangerously_allow_mock_testing_request_params` or a broken
primary (see `probes/README.md` probe 4).
