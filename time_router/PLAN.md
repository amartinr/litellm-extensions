# PLAN — `time_router`

Remaining work. The configuration and robustness items from the previous
revision are implemented: the shared `hook_config` loader, config-driven
targets and peak windows, knobs from `callback_settings`, the hard session
cap, request-body fail-open, non-string session-id handling and offline tests.

## P2 — Routing probes (manual)

Clock routing is verified manually against the real deployment: the
`TIME_ROUTER_FAKE_*` overrides are process environment, not per-request, so an
automated probe would need a local proxy or a gateway restart. Operator-owned;
no automation planned.

## P2 — Route label on the fallback path

`metadata.route` is stamped at request level from the decided target, so a
`router_settings.fallbacks` step can serve a different deployment while the
label reports the decided route. Optional: re-label from the bound deployment,
which would need an `async_pre_call_deployment_hook` in this hook. Out of
scope for the config migration.

## Pending decision — cost-aware switch at the off-peak -> peak boundary

Status: OPEN, needs data. Self-contained; another agent can pick it up here.

### Current behavior

`_pick_route` (in `../time_router.py`) switches an **active** session pinned to
the offpeak target (native) to the peak target (OR) on the first request in a
peak window, and the ratchet keeps OR through the following off-peak. The TTL
only covers idle gaps, so the boundary decision is clock-driven, not
cost-aware.

### The problem

For a long conversation with a **warm native prompt cache**, that immediate
switch may cost more than staying native: it drops the native cache and pays a
full uncached context on OR. At 300k context this is not negligible.

### Cost model

Rates (`../config.yaml.example`, USD/token):

| | input (cold) | cache-read | output |
|---|---|---|---|
| native DeepSeek | 0.00000022 | 0.000000007 | 0.00000066 |
| OR -> Baidu | 0.00000004998 | 0.000000009996 | 0.00000009996 |

- stay native: `N * (C * p_cache + T * p_out)`
- switch to OR: `C * q_in + T * q_out + (N-1) * (C * q_cache + T * q_out)`

with C = context tokens, T = output+reasoning tokens per turn, N = remaining
turns.

### Simulation (C = 300k, warm native cache, config rates assumed effective)

`switch - stay`, positive = switching costs more:

| N \ T | T=0 | T=1000 | T=2000 |
|---|---|---|---|
| 5 | +0.0165 | +0.0137 | +0.0109 |
| 10 | +0.0210 | +0.0154 | +0.0098 |
| 20 | +0.0300 | +0.0188 | +0.0076 |
| 50 | +0.0569 | +0.0289 | +0.0009 |

Base case: switching always costs more and never amortizes, because OR's warm
per-turn input is higher than native's (0.002999 vs 0.002100 at C=300k).

Sensitivity:

- native peak cache-read **x2** -> break-even at ~**6-7 turns** (switch wins
  after);
- OR/Baidu **does not cache** -> OR pays full input every turn; staying wins
  massively (N=10: stay 0.0276 vs switch 0.1509).

### Unknowns to resolve before deciding

1. Native DeepSeek **cache-read rate during peak** (does peak raise it above
   OR's?). This is the pivot: it decides whether warm native stays cheaper.
2. Does the **OR/Baidu route cache** the prompt? If not, switching a large
   context is clearly worse.
3. Typical output/reasoning tokens per turn (OR output is 6.6x cheaper, which
   can flip the per-turn comparison).

### Options

- **A. Keep clock-driven (current).** Simple, deterministic; may overpay on
  long warm-cache conversations.
- **B. Context threshold.** At the boundary keep native when the context
  tokens exceed a configurable `switch_context_tokens` (cache value high),
  else switch. Size derived per request from `data["messages"]`.
- **C. Rate-based break-even.** Compute N* from the rates and switch only if
  expected remaining turns exceed it. Needs the rates and an estimate of
  remaining turns (unknown).
- **D. Park.** Leave the clock rule; revisit if peak spend on long sessions is
  observed to matter.

### Constraints

- Do **not** add a cross-turn token counter to the hook (rejected): the
  conversation is in `data["messages"]` on every request, so any size is
  derived per request, never tracked.
- Keep the boundary logic in `_pick_route`; the TTL only covers idle gaps and
  is currently 900 s.

### How to resolve

1. Get the native peak cache-read rate (DeepSeek price sheet) and confirm
   whether OR/Baidu caches (provider docs or a live probe).
2. Re-run the simulation with real rates.
3. Pick an option. If B, add `switch_context_tokens` to
   `callback_settings.time_router` (+ default, type check, docs, tests) and
   implement the check in `_pick_route`.

Origin: design discussion, 2026-09-12.
