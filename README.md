# LiteLLM TimeRouter Hook — diagnosis workdir

Context: LiteLLM proxy (v1.99.0, container `litellm`, host `172.16.1.1`, DNS `litellm.private`)
runs a custom pre-call hook (`time_router.py`) that time-reroutes the fictional alias
`litellm/deepseek-v4-flash` and injects `metadata.route` so the Prometheus exporter surfaces
it as the `metadata_route` label.

## Status (2026-09-07)

**v2 (direct traffic labeling) VALIDATED LIVE** — 2026-09-07 ~17:1x UTC:

- Hook v2 deployed: labels any request whose model declares `route` in config.yaml
  (`ROUTE_MAP` membership), no rerouting; alias logic unchanged.
- Direct call to `deepseek/deepseek-v4-flash` → `metadata_route="deepseek"`.
- Direct call to `openrouter/deepseek-v4-flash` → `metadata_route="baidu/fp8"`
  (upstream confirmed `provider: Baidu` in the response).
- Agent traffic (`tool=pi`, direct model calls) now carries `metadata_route="deepseek"`.
- Note: metric label `api_provider` on the openrouter group shows the deployment's
  declared provider (`deepseek`), not the real upstream (Baidu via OpenRouter) —
  `metadata_route` is the reliable discriminator.

**v1 (alias reroute label) VALIDATED LIVE** — earlier same day:

- Deployed `time_router.py` to `/app/time_router.py` + `docker restart litellm`.
- One off-peak call to `litellm/deepseek-v4-flash` returned HTTP 200 and produced a new
  series on `litellm_proxy_total_requests_metric_total`:
  `requested_model="deepseek/deepseek-v4-flash" metadata_route="deepseek"`.
- Remaining `metadata_route="None"` series predates the test call (direct call to the real
  model, which bypasses the alias hook — expected).
- Peak-window branch (01:00-04:00 / 06:00-10:00 UTC, route `baidu/fp8`) shares the same
  injection code path; only the ROUTE_MAP value differs (both keys confirmed via
  `/v1/model/info`). Not yet exercised live.

Root cause of `metadata_route="None"` **confirmed against LiteLLM source (master ≈ 1.98/1.99)**:

1. `common_processing_pre_call_logic` runs `add_litellm_data_to_request` **before** custom
   pre-call hooks. Inside it, `litellm_pre_call_utils.py` snapshots the request metadata:
   `data["metadata"]["requester_metadata"] = copy.deepcopy(data["metadata"])`.
2. The standard logging payload whitelists keys (`StandardLoggingMetadata` annotations), so a
   top-level `metadata.route` injected by the hook afterwards is dropped.
3. Prometheus custom labels are built from the merged payload
   (`_get_combined_custom_metadata_from_standard_logging_payload` → `requester_metadata`,
   `spend_logs_metadata`, `user_api_key_auth_metadata`) — not from live top-level request
   metadata. Hence body-supplied `metadata.route` works, hook-injected does not.

**Fix:** write `route` into `metadata["requester_metadata"]["route"]` (and redundantly
`spend_logs_metadata`) inside the hook. See `time_router.py` (deployable) vs
`time_router.orig.py` (deployed version from the handover summary).

## Files

- `time_router.py` — patched hook, ready to deploy to `/app/time_router.py` in the container.
- `time_router.orig.py` — deployed version, for diffing.

## Open items

- LiteLLM issue #38660: failure/fallback metric emitters do not populate custom labels at all
  (they export literal `"None"`), including the `router_settings.fallbacks` path.
- Peak-window branch of the alias (01:00-04:00 / 06:00-10:00 UTC, route `baidu/fp8`) shares
  the same injection code path; not yet exercised live.
