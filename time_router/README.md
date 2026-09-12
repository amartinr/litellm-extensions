# `time_router`

LiteLLM pre-call hook (`CustomLogger`) for the alias
`litellm/deepseek-v4-flash`. Registered as
`time_router.proxy_handler_instance` under `litellm_settings.callbacks`.

## Behavior

1. **Time-based routing** — the alias is rerouted between the deployments
   declared in its `model_info.metadata.time_router.reroute` (`peak_target` /
   `offpeak_target`). The peak schedule is the provider fact declared as
   `model_info.metadata.time_router.peak_windows` on the `offpeak_target`
   entry: `days` 0=Mon..6=Sun (`datetime.weekday()`), `start` / `end` as
   `HH:MM` UTC, interval `[start, end)`; weekends are excluded by omission.
2. **Session stickiness** — while a conversation is active, its provider pin
   is preserved across window boundaries so the provider-side prompt cache is
   not invalidated mid-conversation:
   - active session pinned to the offpeak target crossing into peak → switches
     to the peak target once;
   - active session pinned to the peak target crossing into off-peak → stays
     (one-way ratchet while active);
   - idle beyond `session_ttl_s` (default 3600 s) → pin dropped, routing
     re-evaluates by the clock;
   - requests without a session id fall back to stateless hour-based routing.
3. **Route labeling** — requests for the alias target and for any model in
   `ROUTE_MAP` are stamped with the config-declared route
   (`model_info.metadata.route`). The value is written to
   `metadata.requester_metadata` / `metadata.spend_logs_metadata` so the
   Prometheus exporter surfaces it as `metadata_route` (top-level
   `metadata.route` is dropped by LiteLLM's standard-logging whitelist).
   Unlisted models receive no label.

Direct calls to a model in `ROUTE_MAP` are labeled without rerouting.

## Scope

Time-based routing and session stickiness apply only to entries declaring
`metadata.time_router.reroute` (the alias). Direct deployments never cross
providers and are only route-labeled.

## Session id source

Resolved in order: `data["metadata"]["session_id"]` →
`data["litellm_session_id"]`. Non-string values are ignored. LiteLLM populates
both from the `x-litellm-session-id` request header before pre-call hooks run,
so the client must send it. Example Open WebUI pipe custom-headers valve:

```json
{ "x-litellm-session-id": "{{CHAT_ID}}" }
```

Pipe models bypass the filter pipeline, so body-level stamping never reaches
the outbound payload; use the header.

## Configuration

Read through the shared [`hook_config`](../hook_config.py) loader:

- **Per-model facts** — `model_list[].model_info.metadata`:
  `route` (label), `time_router.reroute` (alias → peak/offpeak targets) and
  `time_router.peak_windows` on the offpeak target entry.
- **Operation knobs** — top-level `callback_settings.time_router`:
  `session_ttl_s` (default 3600) and `max_session_entries` (default 128).

See [`../config.yaml.example`](../config.yaml.example).

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `TIME_ROUTER_DEBUG` | unset | Verbose per-request logging |
| `TIME_ROUTER_FAKE_HOUR` | unset | Override UTC hour (testing window boundaries) |
| `TIME_ROUTER_FAKE_MINUTE` | unset | Override UTC minute (testing `HH:MM` boundaries) |
| `TIME_ROUTER_FAKE_WEEKDAY` | unset | Override weekday, 0=Mon..6=Sun (testing weekends) |
| `LITELLM_CONFIG_FILE` | unset | Config path override (default: `/app/config.yaml`, then `./config.yaml`) |

`session_ttl_s` and `max_session_entries` live in `config.yaml`
(`callback_settings.time_router`), not in the environment.

## Logging

Lazy `verbose_proxy_logger` import (via `hook_config.get_logger`). `STICKY` is
logged when an active session crosses a boundary and the pin overrides the
clock. A hook failure is caught (fail-open) and warned at most once per 5 min.

## Deployment

Mounted at `/app/time_router.py` next to `config.yaml`, alongside
`/app/hook_config.py` and `/app/reasoning_route_adapter.py`. Restart the proxy
after changes.

## Limitations

- Session state is in-memory (per process, single worker). A restart drops
  all pins; multiple workers keep independent state.
- LiteLLM #38660: failure/fallback metric emitters do not populate
  `custom_prometheus_metadata_labels`, so those series export literal
  `"None"`.
- `metadata.route` is stamped from the decided target; a fallback step can
  serve a different deployment (see `PLAN.md`).

## Tests

```
.venv-test/bin/python -m pytest time_router/tests/
```

Offline pytest: schedule parsing and `[start, end)` boundaries, stickiness
and ratchet, labeling, fail-open, and full config wiring via `hook_config`.

## Pending work

See [`PLAN.md`](PLAN.md).
