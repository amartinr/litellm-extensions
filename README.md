# LiteLLM TimeRouter Hook

Custom pre-call hook (`CustomLogger`) for a LiteLLM proxy. Deployed at
`/app/time_router.py` inside the `litellm` container (v1.99.0, no database,
static `config.yaml`).

## Behavior

For the alias `litellm/deepseek-v4-flash`:

1. **Time-based routing** — DeepSeek peak windows (01:00–04:00 and 06:00–10:00 UTC)
   reroute to `openrouter/deepseek-v4-flash`; otherwise to
   `deepseek/deepseek-v4-flash`.
2. **Session stickiness** — while a conversation is active, its provider pin is
   preserved across window boundaries so the provider-side prompt cache is not
   invalidated mid-conversation:
   - Active session pinned to direct crossing into peak → switches to OpenRouter once.
   - Active session pinned to OpenRouter crossing into off-peak → stays on OpenRouter
     (one-way ratchet while active).
   - Idle beyond `SESSION_IDLE_TTL_S` (default 900 s) → pin dropped, routing
     re-evaluates by the clock.
   - Requests without a session id fall back to stateless hour-based routing.
3. **Route labeling** — every request is stamped with the config-declared route
   (`model_info.metadata.route` from `config.yaml`, via `ROUTE_MAP`) so the Prometheus
   exporter surfaces it as the `metadata_route` label. Label propagation requires the
   value in `metadata.requester_metadata` / `metadata.spend_logs_metadata` (top-level
   `metadata.route` is dropped by LiteLLM's standard-logging whitelist).

Direct calls to any model listed in `ROUTE_MAP` are labeled (no rerouting).

## Scope

Time-based routing and session stickiness apply **only** to requests for the alias
`litellm/deepseek-v4-flash`. Clients that call a deployment directly (e.g. the `pi`
agent, which requests `deepseek/deepseek-v4-flash`) never cross providers and are
only route-labeled.

## Session id source

Stickiness keys on the request session id, resolved in this order:
`data["metadata"]["session_id"]` → `data["litellm_session_id"]`.

LiteLLM populates both from the `x-litellm-session-id` request header (during
`add_litellm_data_to_request`, before pre-call hooks run). Therefore the client must
send that header.

In the current deployment, the Open WebUI pipe (`agent_loop_guard`) sends it via its
`GATEWAY_CUSTOM_HEADERS` valve:

```json
{
  "x-litellm-session-id": "{{CHAT_ID}}"
}
```

The `{{CHAT_ID}}` template resolves from the pipe's `__metadata__`. Note that Open
WebUI function filters cannot be used for this: pipe models bypass the filter pipeline,
so body-level stamping never reaches the pipe's outbound payload.

## Configuration

`config.yaml` — no changes required. The hook is already registered under
`litellm_settings.callbacks` (`time_router.proxy_handler_instance`); header
normalization is built into LiteLLM.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `TIME_ROUTER_SESSION_TTL` | `900` | Idle TTL in seconds before a session pin expires |
| `TIME_ROUTER_DEBUG` | unset | Verbose per-request logging |
| `TIME_ROUTER_FAKE_HOUR` | unset | Override UTC hour (testing window boundaries) |

## Logging

All hook logs go through LiteLLM's `verbose_proxy_logger` and therefore match the
proxy's JSON log format when `json_logs: true`. The `STICKY` line is always emitted
when an active session crosses a boundary and the pin overrides the clock.

## Deployment

```bash
sudo docker cp time_router.py litellm:/app/time_router.py
sudo docker restart litellm
```

## Limitations

- Session state is **in-memory** (per process, single worker). A proxy restart drops
  all pins; the next request re-pins by the clock.
- Multiple workers would each keep their own pin state (not shared).
- LiteLLM issue #38660: failure/fallback metric emitters do not populate
  `custom_prometheus_metadata_labels`, so those series export literal `"None"`.
