# `time_router`

LiteLLM pre-call hook (`CustomLogger`) for the alias
`litellm/deepseek-v4-flash`. Registered as
`time_router.proxy_handler_instance` under `litellm_settings.callbacks`.

## Behavior

1. **Time-based routing** — DeepSeek peak windows (Mon-Fri 01:00-04:00 and
   06:00-10:00 UTC; weekends always off-peak) reroute to
   `openrouter/deepseek-v4-flash`; otherwise to
   `deepseek/deepseek-v4-flash`.
2. **Session stickiness** — while a conversation is active, its provider pin
   is preserved across window boundaries so the provider-side prompt cache is
   not invalidated mid-conversation:
   - active session pinned to direct crossing into peak → switches to
     OpenRouter once;
   - active session pinned to OpenRouter crossing into off-peak → stays on
     OpenRouter (one-way ratchet while active);
   - idle beyond `SESSION_IDLE_TTL_S` (default 900 s) → pin dropped, routing
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

Time-based routing and session stickiness apply only to
`litellm/deepseek-v4-flash`. Direct deployments never cross providers and are
only route-labeled.

## Session id source

Resolved in order: `data["metadata"]["session_id"]` →
`data["litellm_session_id"]`. LiteLLM populates both from the
`x-litellm-session-id` request header before pre-call hooks run, so the client
must send it. Example Open WebUI pipe custom-headers valve:

```json
{ "x-litellm-session-id": "{{CHAT_ID}}" }
```

Pipe models bypass the filter pipeline, so body-level stamping never reaches
the outbound payload; use the header.

## Configuration

`ROUTE_MAP` is built from `config.yaml` →
`model_list[].model_info.metadata.route` (see
[`../config.yaml.example`](../config.yaml.example)). The reroute targets
(`PEAK_TARGET`, `OFFPEAK_TARGET`) are constants in the module.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `TIME_ROUTER_SESSION_TTL` | `900` | Idle TTL in seconds before a session pin expires |
| `TIME_ROUTER_DEBUG` | unset | Verbose per-request logging |
| `TIME_ROUTER_FAKE_HOUR` | unset | Override UTC hour (testing window boundaries) |
| `TIME_ROUTER_FAKE_WEEKDAY` | unset | Override weekday, 0=Mon..6=Sun (testing weekends) |
| `LITELLM_CONFIG_FILE` | unset | Config path override (default: `/app/config.yaml`, then `./config.yaml`) |

## Logging

Lazy `verbose_proxy_logger` import. `STICKY` is logged when an active session
crosses a boundary and the pin overrides the clock.

## Deployment

Mounted at `/app/time_router.py` next to `config.yaml`. Restart the proxy
after changes.

## Limitations

- Session state is in-memory (per process, single worker). A restart drops
  all pins; multiple workers keep independent state.
- LiteLLM #38660: failure/fallback metric emitters do not populate
  `custom_prometheus_metadata_labels`, so those series export literal
  `"None"`.

## Tests and probes

`tests/` and `probes/` are placeholders — no offline tests or live probes for
this hook yet. See [`PLAN.md`](PLAN.md).
