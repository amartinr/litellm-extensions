#!/usr/bin/env python3
"""Live gateway contract check for the per-deployment reasoning adapter.

Verifies, against a running LiteLLM gateway, that the adapter (now on
`async_pre_call_deployment_hook`) classifies the **bound deployment** and
normalizes the reasoning dialect end-to-end:

- OR deployment + native kill switch (`thinking:{type:disabled}`) -> rescued to
  the OR OFF object -> **0 reasoning tokens**. This is also the check that the
  bound deployment's `model_info.metadata` / `deployment_model_name` reaches
  the hook (DESIGN.md §4.2/§4.3).
- Native deployment + the same kill switch -> honored natively -> 0 tokens.
- No reasoning control -> reasons by default on both routes (sanity).

Strictly env-driven; no infrastructure values are hardcoded or stored:

    LITELLM_BASE                  gateway base URL (default http://litellm.private)
    LITELLM_KEY | LITELLM_MASTER_KEY | GATEWAY_API_KEY
    LITELLM_SPEND_LOGS_METADATA   optional JSON sent as
                                  x-litellm-spend-logs-metadata (keeps test
                                  traffic out of production metrics)

Usage:
    LITELLM_SPEND_LOGS_METADATA='{...}' .venv-test/bin/python \
        reasoning_route_adapter/probes/gateway_contract.py

Not covered here: the fallback path (native failure -> OR). Forcing it needs
`general_settings.dangerously_allow_mock_testing_request_params` (off on the
shared gateway), a broken primary, or a real provider failure. The per-attempt
re-normalization is covered offline in
`tests/test_reasoning_route_adapter.py::test_fallback_attempt_renormalizes`.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://litellm.private")
MAX_TOKENS = 256
PROMPT = "Which number is larger, 9.11 or 9.8? Reply in one word."

# Per-request nonce so a gateway response cache cannot mask the adapter
# (a cached body would return without the hook running).

OR = "openrouter/deepseek-v4-flash"
NATIVE = "deepseek/deepseek-v4-flash"

# leg id -> (model, extra body, expected reasoning)
LEGS = [
    ("or_off", OR, {"thinking": {"type": "disabled"}}, False),
    ("or_default", OR, {}, True),
    ("native_off", NATIVE, {"thinking": {"type": "disabled"}}, False),
    ("native_default", NATIVE, {}, True),
]


def _api_key() -> str:
    for name in ("LITELLM_KEY", "LITELLM_MASTER_KEY", "GATEWAY_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    raise SystemExit("No API key found in LITELLM_KEY / LITELLM_MASTER_KEY / GATEWAY_API_KEY.")


def _headers() -> dict:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {_api_key()}"}
    spend = os.environ.get("LITELLM_SPEND_LOGS_METADATA")
    if spend:
        headers["x-litellm-spend-logs-metadata"] = spend
    return headers


def chat(model: str, extra: dict):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": f"{PROMPT} (ref {time.time_ns()})"}],
        "max_tokens": MAX_TOKENS,
        "stream": False,
        **extra,
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers=_headers(),
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode()), time.time() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read().decode(errors="replace") or "")[:300], time.time() - t0


def reasoning_tokens(data: dict) -> int:
    usage = data.get("usage") or {}
    det = usage.get("completion_tokens_details") or {}
    return int(det.get("reasoning_tokens") or 0)


def run_leg(model: str, extra: dict) -> dict:
    status, data, secs = chat(model, extra)
    if status != 200:
        return {"status": status, "reasoned": None, "tokens": 0, "secs": secs, "note": str(data)[:160]}
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    tokens = reasoning_tokens(data)
    reasoned = tokens > 0 or bool(msg.get("reasoning_content"))
    return {"status": status, "reasoned": reasoned, "tokens": tokens, "secs": secs, "note": ""}


def main() -> int:
    print(f"gateway: {BASE}")
    print(f"spend-logs header: {'set' if os.environ.get('LITELLM_SPEND_LOGS_METADATA') else 'NOT SET'}\n")
    failures = 0
    for leg_id, model, extra, expected in LEGS:
        result = run_leg(model, extra)
        ok = result["reasoned"] is expected
        failures += 0 if ok else 1
        verdict = "PASS" if ok else "FAIL"
        print(
            f"{verdict}  {leg_id:<16} model={model:<32} "
            f"expected_reasoning={expected!s:<5} got={result['reasoned']!s:<5} "
            f"reasoning_tokens={result['tokens']:<4} ({result['secs']:.1f}s) {result['note']}"
        )
    print(f"\n{len(LEGS) - failures}/{len(LEGS)} legs ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
