# PLAN — `time_router`

Pending work, ordered by priority. From a code/documentation review; none of
these is implemented yet.

## Configuration target (reference for the items below)

Two surfaces, corrected against LiteLLM v1.99.0:

- **Per-model facts** → `model_info.metadata` (the hook already reads `route`
  from there). Everything hook-owned nests under a `time_router` sub-dict;
  individual keys are not prefixed.
- **Hook operation knobs** → `callback_settings.time_router` at the **top
  level** of `config.yaml`. v1.99.0 reads `config.get("callback_settings")`
  in `proxy_server.py` and exposes it as `litellm.callback_settings`; it is
  NOT `litellm_settings.callback_settings`. A `CustomLogger` registered by
  dotted path does **not** receive its block automatically (only built-in
  callbacks get `callback_specific_params` via `initialize_callbacks_on_proxy`),
  so the hook must read it itself. Decision: read it from `config.yaml` in the
  shared loader — uniform with the metadata reader, independent of LiteLLM
  load order, offline-testable.

```yaml
model_list:
  # The alias: declares the DECISION (that it reroutes, and between which
  # targets). Identified by the presence of `time_router.reroute`.
  - model_name: litellm/deepseek-v4-flash
    model_info:
      metadata:
        route: "baidu/fp8"           # label of its own default deployment
        reasoning_dialect: "openrouter"
        time_router:
          reroute:
            peak_target: "openrouter/deepseek-v4-flash"
            offpeak_target: "deepseek/deepseek-v4-flash"

  # The native entry: OWNS the provider schedule (DeepSeek peak pricing).
  - model_name: deepseek/deepseek-v4-flash
    model_info:
      metadata:
        route: "deepseek"
        reasoning_dialect: "deepseek"
        time_router:
          peak_windows_utc:          # 0=Mon..6=Sun (datetime.weekday())
            - days: [0, 1, 2, 3, 4]  # weekend excluded by omission
              start: "01:00"         # interval is [start, end), UTC
              end: "04:00"
            - days: [0, 1, 2, 3, 4]
              start: "06:00"
              end: "10:00"

callback_settings:                   # top level, NOT under litellm_settings
  time_router:
    session_ttl_s: 900
    max_session_entries: 128
```

Resolution rules (implement exactly):
- The alias is identified by the **presence of
  `metadata.time_router.reroute`**, not by comparing `data["model"]` to
  `ALIAS_MODEL`; the literal is removed.
- The peak schedule is read from the entry named by `offpeak_target`
  (`deepseek/deepseek-v4-flash`), the entry that owns the native-provider
  fact — not from the alias and not from the hook.
- `route = peak_target if is_peak(now) else offpeak_target`; weekends
  disappear by omission (the `weekday >= 5` branch is removed).
- `PEAK_TARGET` / `OFFPEAK_TARGET` literals are removed.

## Configuration error policy

- Unreadable / invalid YAML → empty descriptors; the hook no-ops; log once at
  ERROR. A hook config problem never aborts proxy startup.
- Parseable but semantically invalid routing config (target not in
  `model_list`, `offpeak_target` without `peak_windows_utc`, malformed
  window) → do not reroute (leave `data["model"]` untouched), and log an
  ERROR at load listing every problem. Never emit an invalid model and never
  the `route="unknown"` label.
- Rationale: replaces the current silent request-time 400 with a loud
  load-time diagnostic while honouring the existing "a hook must never fail
  the request" P0. The source suggestion proposed aborting startup instead;
  not adopted — an optional pre-call hook should not take down the gateway.

## P0 — Offline tests

`tests/` is empty. The decision logic is near-pure and cheap to test:

- schedule parsing: `days` / `start` / `end`, `[start, end)` boundaries,
  weekday mapping (0=Mon), weekend by omission;
- `is_peak` at exact boundaries (`01:00`, `04:00`, `06:00`, `10:00`);
- `_desired_route`: peak vs off-peak;
- `_pick_route`: new session, idle expiry, direct→peak switch, OpenRouter
  ratchet, no session id;
- `_inject_route`: writes `metadata.route`, `requester_metadata.route`,
  `spend_logs_metadata.route`;
- `_utc_hour` / `_utc_weekday` overrides.

The window schema allows minutes, so the fake clock must too: add
`TIME_ROUTER_FAKE_MINUTE` (or make the decision functions take an injected
`now`) — `TIME_ROUTER_FAKE_HOUR` alone cannot exercise `HH:MM` boundaries.

Target: `tests/test_time_router.py`, stdlib only (stub `yaml` and `litellm`
as in `../reasoning_route_adapter/tests/test_reasoning_route_adapter.py`).

## P0 — Fail-open (request body)

`async_pre_call_hook` has no `try/except` (unlike the adapter). Concrete
failure paths:

- non-string `metadata.session_id` → `session_id[:12]` raises in the `STICKY`
  log;
- non-dict `metadata` → `_inject_route`'s `setdefault` raises.

Wrap the hook body; a hook must never fail the request. (Config-load failures
are covered by the error policy above.)

## P1 — Config-driven reroute targets + load-time validation

See "Configuration target" and "Configuration error policy". Replaces the
current P1 of the same name; also removes the hardcoded peak windows and the
weekend branch. On load, assert that every `reroute.peak_target` /
`offpeak_target` names an existing `model_list` entry, and that the
`offpeak_target` entry declares non-empty `peak_windows_utc` with
`days ⊆ 0..6`, `start`/`end` parseable `HH:MM`, and `start < end`.

## P1 — Hook knobs from `callback_settings`

Move `session_ttl_s` (replacing `TIME_ROUTER_SESSION_TTL`, default 900) and
the `_prune_sessions` cap `max_session_entries` (default 128) to
`callback_settings.time_router`. Document the defaults; type-check with a
loud ERROR and fall back to the default. Update `time_router/README.md` and
`.env.example` (remove the moved env var).

## P1 — Hard cap on session state

`_prune_sessions` only removes expired entries and only when `len > 128`;
with many concurrent active sessions the dict grows without bound. Add a hard
cap / LRU eviction reading `max_session_entries` from `callback_settings`.

## P1 — Shared loader

Decision: extract `CONFIG_PATHS` / config-load / `_log` plus the new
`callback_settings` reader and validation into one module, `hook_config.py`,
mounted at `/app/hook_config.py` and imported as top-level `hook_config` by
both hooks. API: `MODELS` (`{model_name: {route, reasoning_dialect, time_router}}`),
`settings(hook_name)` (`callback_settings.<hook>` with defaults/types),
`get_logger()`. Rationale: config I/O, validation and the
settings reader exist once, so each hook keeps only its policy and they
cannot diverge; it is also the single test surface. Not a net line reduction
once validation/settings are included — the win is single-source behavior,
not LOC.

Rejected alternative: `reasoning_route_adapter` importing from
`time_router` (avoids the third mount but couples the hooks). Cost of the
chosen option: a third volume mount — update the root `README.md` `volumes:`
block. `/app` is on `sys.path` at runtime (LiteLLM's CLI appends
`os.getcwd()`), so the import resolves. Offline tests stub `hook_config`
instead of `yaml`/`litellm`. Recorded in `reasoning_route_adapter/DESIGN.md`
§5.2.

## P1 — Keep the routing decision at request level

`time_router` stays on `async_pre_call_hook`: it chooses the deployment by
rewriting `data["model"]` before routing, which is impossible at the
deployment hook (the deployment is already selected). The adapter moves to
`async_pre_call_deployment_hook` (`reasoning_route_adapter/PLAN.md` P0,
`DESIGN.md` §7), so the "ORDER MATTERS: time_router first" note in
`config.yaml.example` / README no longer affects it; update that note to
avoid confusion. Optional, out of scope: re-label `metadata.route` at the
deployment hook so a fallback does not stamp the decided route instead of the
served one.

## P2 — Routing probes

`probes/` is empty. Add a live probe for the alias: assert the served provider
for a given `TIME_ROUTER_FAKE_HOUR` / `TIME_ROUTER_FAKE_WEEKDAY`, and the
sticky behavior across two requests sharing a session id.

## P2 — Session id validation

Reject or normalize a non-string session id instead of relying on the caller.
