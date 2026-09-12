# PLAN — `time_router`

Remaining work. The configuration and robustness items from the previous
revision are implemented: the shared `hook_config` loader, config-driven
targets and peak windows, knobs from `callback_settings`, the hard session
cap, request-body fail-open, non-string session-id handling and offline tests.

## P2 — Routing probes

`probes/` is empty. Add a live probe for the alias: assert the served provider
for a given `TIME_ROUTER_FAKE_HOUR` / `TIME_ROUTER_FAKE_MINUTE` /
`TIME_ROUTER_FAKE_WEEKDAY`, and the sticky behavior across two requests
sharing a session id. Env-gated (`LITELLM_BASE`, key); see the root README
"Retries and fallback" for the fallback interaction.

## P2 — Route label on the fallback path

`metadata.route` is stamped at request level from the decided target, so a
`router_settings.fallbacks` step can serve a different deployment while the
label reports the decided route. Optional: re-label from the bound deployment,
which would need an `async_pre_call_deployment_hook` in this hook. Out of
scope for the config migration.
