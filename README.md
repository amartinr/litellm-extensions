# LiteLLM TimeRouter Hook

Custom pre-call hook (`CustomLogger`) for a LiteLLM proxy. Deployed at
`/app/time_router.py` inside the `litellm` container (v1.99.0, no database,
static `config.yaml`).

## Behavior

For the alias `litellm/deepseek-v4-flash`:

1. **Time-based routing** - DeepSeek peak windows (Mon-Fri 01:00-04:00 and
   06:00-10:00 UTC; weekends have no peak pricing and are always off-peak)
   reroute to `openrouter/deepseek-v4-flash`; otherwise to
   `deepseek/deepseek-v4-flash`.
2. **Session stickiness** - while a conversation is active, its provider pin is
   preserved across window boundaries so the provider-side prompt cache is not
   invalidated mid-conversation:
   - Active session pinned to direct crossing into peak → switches to OpenRouter once.
   - Active session pinned to OpenRouter crossing into off-peak → stays on OpenRouter
     (one-way ratchet while active).
   - Idle beyond `SESSION_IDLE_TTL_S` (default 900 s) → pin dropped, routing
     re-evaluates by the clock.
   - Requests without a session id fall back to stateless hour-based routing.
3. **Route labeling** - every request is stamped with the config-declared route
   (`model_info.metadata.route` from `config.yaml`, via `ROUTE_MAP`) so the Prometheus
   exporter surfaces it as the `metadata_route` label. Label propagation requires the
   value in `metadata.requester_metadata` / `metadata.spend_logs_metadata` (top-level
   `metadata.route` is dropped by LiteLLM's standard-logging whitelist).

Direct calls to any model listed in `ROUTE_MAP` are labeled (no rerouting).

## Scope

Time-based routing and session stickiness apply **only** to requests for the alias
`litellm/deepseek-v4-flash`. Clients that call a deployment directly never cross
providers and are only route-labeled - whether a client participates in rerouting
depends on the model name it targets (e.g. pi can use the alias to benefit from
rerouting, or request the gateway's direct `deepseek/deepseek-v4-flash` to stay
pinned to native). The hooks run on every request that reaches the gateway -
including pi when LiteLLM is configured in it as a `deepseek` provider (that
provider's base_url is the gateway; its model name decides the treatment). The
only config the hooks never see is a provider that points straight at
`api.deepseek.com` with no gateway in the path, which needs no normalization
(native dialect against the native API) and gains no peak-price avoidance.

## Session id source

Stickiness keys on the request session id, resolved in this order:
`data["metadata"]["session_id"]` → `data["litellm_session_id"]`.

LiteLLM populates both from the `x-litellm-session-id` request header (during
`add_litellm_data_to_request`, before pre-call hooks run). Therefore the client must
send that header. For example, an Open WebUI pipe can be configured to send it via a
custom-headers valve templated with the chat id:

```json
{
  "x-litellm-session-id": "{{CHAT_ID}}"
}
```

The template resolves from the pipe's metadata. Note that Open
WebUI function filters cannot be used for this: pipe models bypass the filter pipeline,
so body-level stamping never reaches the pipe's outbound payload.

## Configuration

`config.yaml` — no changes required to run the hook beyond registration (see
[`config.yaml.example`](config.yaml.example) for a full reference with the required
`model_info.metadata.route` entries). Keys are referenced as `os.environ/...`.

The hook is registered under `litellm_settings.callbacks`
(`time_router.proxy_handler_instance`); header normalization is built into LiteLLM.

### Environment

Required (see [`.env.example`](.env.example)):

| Variable | Purpose |
|---|---|
| `LITELLM_MASTER_KEY` | Admin key. Required by the proxy; with a DB-less setup every client authenticates with it |
| `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` | Provider keys used by `config.yaml.example` (`os.environ/...` references) |

Hook knobs (optional):

| Variable | Default | Purpose |
|---|---|---|
| `TIME_ROUTER_SESSION_TTL` | `900` | Idle TTL in seconds before a session pin expires |
| `TIME_ROUTER_DEBUG` | unset | Verbose per-request logging |
| `TIME_ROUTER_FAKE_HOUR` | unset | Override UTC hour (testing window boundaries) |
| `TIME_ROUTER_FAKE_WEEKDAY` | unset | Override weekday, 0=Mon..6=Sun (testing weekends) |

## Logging

All hook logs go through LiteLLM's `verbose_proxy_logger` and therefore match the
proxy's JSON log format when `json_logs: true`. The `STICKY` line is always emitted
when an active session crosses a boundary and the pin overrides the clock.

## Deployment

The hook is imported by LiteLLM as the module `time_router`, so it must live in the
same directory as `config.yaml` (the proxy working directory). In Docker that is
`/app` - mount the file there (e.g. `-v ./time_router.py:/app/time_router.py`) and
restart the container after changes.

## Companion hook — `reasoning_route_adapter`

Clients (Open WebUI and pi, via their extensions) always send the
DeepSeek-native reasoning dialect (root `thinking`/`reasoning_effort`,
`reasoning_content` on assistant messages). When such traffic lands on the
OpenRouter route, that dialect is mishandled: OR ignores `thinking` (a
`thinking:{type:"disabled"}` still reasons and bills) and only honors its own
`reasoning` object. `reasoning_route_adapter.py` (registered AFTER
`time_router` in `callbacks`, since it classifies by the rerouted model)
normalizes the payload to the dialect of the bound route - see `DESIGN.md`
for the rules, evidence and acceptance criteria; `test_reasoning_route_adapter.py`
runs the offline acceptance checks offline (stdlib only, no network).

## Limitations

- Session state is **in-memory** (per process, single worker). A proxy restart drops
  all pins; the next request re-pins by the clock.
- Multiple workers would each keep their own pin state (not shared).
- LiteLLM issue #38660: failure/fallback metric emitters do not populate
  `custom_prometheus_metadata_labels`, so those series export literal `"None"`.
