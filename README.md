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
hook_config.py                            shared config loader (pending; mount at /app/hook_config.py)
.env.example                              shared environment reference

time_router/
  time_router.py                          hook (mount at /app/time_router.py)
  README.md  PLAN.md
  tests/                                  placeholder
  probes/                                 placeholder

reasoning_route_adapter/
  reasoning_route_adapter.py              hook (mount at /app/reasoning_route_adapter.py)
  DESIGN.md  README.md  PLAN.md
  tests/test_reasoning_route_adapter.py   offline acceptance checks
  probes/                                 live probes + recorded results
```

## Deployment

LiteLLM imports the files as top-level modules `time_router` and
`reasoning_route_adapter`, so both must sit next to `config.yaml` in the proxy
working directory (`/app` in Docker). Example mounts:

```yaml
volumes:
  - ./config.yaml:/app/config.yaml
  - ./time_router/time_router.py:/app/time_router.py
  - ./reasoning_route_adapter/reasoning_route_adapter.py:/app/reasoning_route_adapter.py
```

When the shared config loader lands (`hook_config.py`, see PLAN), add it as a
third mount so both hooks can `import hook_config`:
`- ./hook_config.py:/app/hook_config.py`.

Registration order: `time_router` first (it reroutes the alias at request
level). Today `reasoning_route_adapter` reads the rerouted model, so the order
matters; the pending migration to the per-deployment hook
([`reasoning_route_adapter/PLAN.md`](reasoning_route_adapter/PLAN.md)) makes it
order-independent (it will classify by the bound deployment). See
[`config.yaml.example`](config.yaml.example).

## Environment

`LITELLM_MASTER_KEY`, `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY` are
required; `LITELLM_CONFIG_FILE` overrides the config path for both hooks. Hook
knobs are documented in each hook's README. Copy
[`.env.example`](.env.example) to `.env`.

## Cross-cutting follow-ups

- Extract the duplicated `CONFIG_PATHS` / config-load / `_log` plumbing shared
  by both hooks into a common module (`hook_config`), together with the
  `model_info.metadata` descriptor builder, the top-level `callback_settings`
  reader and its validation. See `time_router/PLAN.md` and
  `reasoning_route_adapter/DESIGN.md` §5.2.
- Re-validate on LiteLLM upgrades: the hooks depend on `verbose_proxy_logger`,
  the standard-logging metadata whitelist, and pre-call hook semantics
  (pinned to 1.99.0).
