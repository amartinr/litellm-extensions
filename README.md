# litellm-extensions

Monorepo of LiteLLM pre-call hooks for a DB-less proxy (v1.99.0) in front of
DeepSeek (native) and OpenRouter → Baidu fp8.

## Hooks

| Hook | Directory | Purpose |
|---|---|---|
| `time_router` | [`time_router/`](time_router/) | Clock-based rerouting of the `litellm/deepseek-v4-flash` alias, session stickiness, and `metadata.route` labeling |
| `reasoning_route_adapter` | [`reasoning_route_adapter/`](reasoning_route_adapter/) | Normalizes reasoning control to the dialect of the bound route |

Each hook has its own `README.md` (behavior, configuration, environment) and
`PLAN.md` (pending work). Design rationale and measured evidence for the
adapter live in
[`reasoning_route_adapter/DESIGN.md`](reasoning_route_adapter/DESIGN.md).

## Layout

```
config.yaml.example                       shared LiteLLM config reference (both hooks)
hook_config.py                            shared config loader (mount at /app/hook_config.py)
.env.example                              shared environment reference

time_router/
  time_router.py                          hook (mount at /app/time_router.py)
  README.md  PLAN.md
  tests/test_time_router.py               pytest (offline)
  probes/                                 placeholder

reasoning_route_adapter/
  reasoning_route_adapter.py              hook (mount at /app/reasoning_route_adapter.py)
  DESIGN.md  README.md  PLAN.md
  tests/test_reasoning_route_adapter.py   pytest (offline)
  probes/                                 live probes + recorded results
```

## Deployment

LiteLLM imports the files as top-level modules `time_router` and
`reasoning_route_adapter`, so both must sit next to `config.yaml` in the proxy
working directory (`/app` in Docker). Example mounts:

```yaml
volumes:
  - ./config.yaml:/app/config.yaml
  - ./hook_config.py:/app/hook_config.py
  - ./time_router/time_router.py:/app/time_router.py
  - ./reasoning_route_adapter/reasoning_route_adapter.py:/app/reasoning_route_adapter.py
```

`hook_config.py` is imported by both hooks; it must be mounted or the proxy
fails to import the hook.

Registration order: `time_router` first (it reroutes the alias at request
level). `reasoning_route_adapter` classifies by the bound deployment
(`async_pre_call_deployment_hook`), so its position in the list no longer
matters. See [`config.yaml.example`](config.yaml.example).

## Retries and fallback (LiteLLM core)

Core router settings, not hook config: the hooks neither read nor define
them. They matter because they decide when a request reaches OpenRouter via
`router_settings.fallbacks`, i.e. when the adapter's per-deployment hook runs
against the fallback deployment. Defaults are v1.99.0.

| Setting | Default | Effect |
|---|---|---|
| `litellm_params.num_retries` (per model) | unset → router default (2); native entry sets 2 | retryable errors (408/409/429/5xx) are retried before `fallbacks`; every retried attempt still runs the per-deployment hook |
| `litellm_params.timeout` (per model) | provider default; native entry sets 10 s | per-attempt timeout |
| `router_settings.num_retries` | 2 (`openai.DEFAULT_MAX_RETRIES`) | router-wide retry count, used when the deployment does not set its own |
| `router_settings.allowed_fails` | 3 | failures/minute before a deployment enters cooldown |
| `router_settings.cooldown_time` | 5 s | a cooled-down deployment is skipped; with a single native deployment the group has none healthy, so requests fall back for the cooldown window |
| `router_settings.fallbacks` | — | the only path that sends native-group traffic to OR; non-retryable errors (400/401/403/404) fall back immediately |

See [`reasoning_route_adapter/DESIGN.md`](reasoning_route_adapter/DESIGN.md) §7
for the adapter consequence.

## Environment

`LITELLM_MASTER_KEY`, `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY` are
required; `LITELLM_CONFIG_FILE` overrides the config path for both hooks. Hook
knobs are documented in each hook's README. Copy
[`.env.example`](.env.example) to `.env`.

## Cross-cutting follow-ups

- Shared `hook_config` module (per-model descriptors, `callback_settings`
  reader, validation): implemented and wired into both hooks.
- Re-validate on LiteLLM upgrades: the hooks depend on `verbose_proxy_logger`,
  the standard-logging metadata whitelist, and pre-call hook semantics
  (pinned to 1.99.0).
