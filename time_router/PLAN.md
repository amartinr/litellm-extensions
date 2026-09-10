# PLAN — `time_router`

Pending work, ordered by priority. From a code/documentation review; none of
these is implemented yet.

## P0 — Offline tests

No tests exist for the hook with the most state. The decision logic is
near-pure and cheap to test:

- `_is_peak(hour, weekday)`: both windows and their boundaries, weekends.
- `_desired_route`: peak vs off-peak.
- `_pick_route`: new session, idle expiry, direct→peak switch, OpenRouter
  ratchet, no session id.
- `_inject_route`: writes `metadata.route`, `requester_metadata.route`,
  `spend_logs_metadata.route`.
- `_utc_hour` / `_utc_weekday`: `TIME_ROUTER_FAKE_*` overrides.

Target: `tests/test_time_router.py`, stdlib only (stub `yaml` and `litellm`
as in `../reasoning_route_adapter/tests/test_reasoning_route_adapter.py`).

## P0 — Fail-open

`async_pre_call_hook` has no `try/except` (unlike the adapter). Concrete
failure paths:

- non-string `metadata.session_id` → `session_id[:12]` raises in the `STICKY`
  log;
- non-dict `metadata` → `_inject_route`'s `setdefault` raises;
- malformed `config.yaml` → `_load_route_map` raises at import time.

Wrap the hook body and the config load; a hook must never fail the request.

## P1 — Hard cap on session state

`_prune_sessions` only removes expired entries and only when
`len > 128`. With many concurrent active sessions the dict grows without
bound. Add a hard cap / LRU eviction.

## P1 — Config-driven reroute targets

`PEAK_TARGET` / `OFFPEAK_TARGET` are hardcoded while route labels come from
`config.yaml`. Renaming a deployment yields `route="unknown"` and a possibly
invalid `model` (400). Derive or validate the targets at import.

## P2 — Routing probes

`probes/` is empty. Add a live probe for the alias: assert the served provider
for a given `TIME_ROUTER_FAKE_HOUR` / `TIME_ROUTER_FAKE_WEEKDAY`, and the
sticky behavior across two requests sharing a session id.

## P2 — Session id validation

Reject or normalize a non-string session id instead of relying on the caller.
