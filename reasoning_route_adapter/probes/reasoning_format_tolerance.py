#!/usr/bin/env python3
"""Probe 1 — reasoning-format contract monitor, LiteLLM -> OpenRouter -> Baidu.

Contract under test (deepseek/deepseek-v4-flash-0731 via OpenRouter)
--------------------------------------------------------------------
OR exposes per-model reasoning metadata in GET /api/v1/models (queried
2026-09-08, no key needed):

    "reasoning": {
      "mandatory": false,
      "default_enabled": true,
      "supported_efforts": ["max", "high", "low"],
      "default_effort": "high"
    }

Payload rule (user directive): always send explicit key names and values;
never rely on inference. The reasoning control is the OR object with BOTH
keys, "enabled" and "effort", even when redundant (effort "none" is sent
alongside enabled false). No root-level spellings are used (root
reasoning_effort and the thinking object were probed and removed on
2026-09-08 — root reasoning_effort:"none" does not disable on this route,
and thinking:{type:disabled} is ignored; see git history and README).

- supported_efforts == DeepSeek's native 3 levels (low/high/max). DeepSeek's
  own docs collapse the wider OR vocabulary: medium->high, xhigh->high.
- History/root spellings ruled out on 2026-09-08 (see README, Results):
  thinking:{type:disabled} is ignored on this route; root
  reasoning_effort:"none" does not disable. The reasoning object with
  explicit keys (enabled + effort) is the only control used.

Each leg is one short request. A leg marked UNEXPECTED means the endpoint
drifted from the contract above.

Constraints
-----------
- API key from env only (LITELLM_KEY or LITELLM_MASTER_KEY); refuses to run
  without it. Never hardcoded.
- Reasoning-on legs use effort "low" (credit budget). LITELLM_EFFORT
  overrides. Off legs cost ~nothing.
- One provider per run. Default target model: openrouter/deepseek-v4-flash
  (LiteLLM alias -> OR deepseek-v4-flash-0731 -> Baidu fp8, provider.order
  ["baidu/fp8"], no fallbacks). Native comparison:
  LITELLM_MODEL=deepseek/deepseek-v4-flash (same contract probe on the
  direct-API route).
- No cross-provider histories: every request is single-turn and standalone.

Usage
-----
    .venv/bin/python reasoning_route_adapter/probes/reasoning_format_tolerance.py [leg ...]
    # default = all legs (3 requests); pass leg ids for a subset:
    .venv/bin/python reasoning_route_adapter/probes/reasoning_format_tolerance.py off
    LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python reasoning_route_adapter/probes/....py

Cost
----
3 requests, short prompt, max_tokens=256, effort low (off legs ~zero) =>
well under $0.001 on the OpenRouter route.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://litellm.private")
MODEL = os.environ.get("LITELLM_MODEL", "openrouter/deepseek-v4-flash")
EFFORT = os.environ.get("LITELLM_EFFORT", "low")  # always low for Baidu
MAX_TOKENS = 256

# One short prompt that reliably triggers (cheap) chain-of-thought.
PROMPT = "Which number is larger, 9.11 or 9.8? Reply in one word."


def _api_key() -> str:
    for name in ("LITELLM_KEY", "LITELLM_MASTER_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    raise SystemExit(
        "No API key found: set LITELLM_KEY or LITELLM_MASTER_KEY "
        "(the key is never hardcoded in these probes)."
    )


KEY = _api_key()

# leg id -> (extra body params, expected reasoning, description)
# Payload rule (user directive): always send explicit key names and values.
# The reasoning control is the OR object with BOTH keys — "effort" and
# "enabled" — even when redundant (effort none + enabled false). No
# root-level spellings are probed (root reasoning_effort / thinking were
# removed; see git history for the 2026-09-08 findings that ruled them out).
LEGS = [
    ("none", {}, True,
     "no reasoning params (OR metadata: default enabled, effort high)"),
    ("on_low", {"reasoning": {"enabled": True, "effort": EFFORT}}, True,
     "OR object, explicit keys: enabled true + effort low (contract form)"),
    ("off", {"reasoning": {"enabled": False, "effort": "none"}}, False,
     "OR object, explicit keys: enabled false + effort none, sent both "
     "(contract form, redundant on purpose)"),
]


def chat(extra_params: dict):
    """POST one non-stream chat completion. Returns (status, json|err_text)."""
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
        "stream": False,
        **extra_params,
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {KEY}",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode()), time.time() - t0
    except urllib.error.HTTPError as exc:
        snippet = (exc.read().decode(errors="replace") or "")[:300]
        return exc.code, snippet, time.time() - t0


def reasoning_signals(message: dict, usage: dict):
    """Which response fields carry reasoning, and does any reasoning exist?"""
    present = []
    rc = message.get("reasoning_content")
    if isinstance(rc, str) and rc:
        present.append("reasoning_content")
    rs = message.get("reasoning")
    if isinstance(rs, str) and rs:
        present.append("reasoning")
    psf = message.get("provider_specific_fields") or {}
    p_rs = psf.get("reasoning")
    if isinstance(p_rs, str) and p_rs:
        present.append("provider_specific_fields.reasoning")
    if isinstance(psf.get("reasoning_details"), list) and psf["reasoning_details"]:
        present.append("provider_specific_fields.reasoning_details")
    det = (usage or {}).get("completion_tokens_details") or {}
    r_tokens = int(det.get("reasoning_tokens") or 0)
    if r_tokens > 0:
        present.append("usage.reasoning_tokens")
    reasoned = bool(present)
    rc_len = len(rc) if isinstance(rc, str) else 0
    return reasoned, rc_len, r_tokens, present


def run_leg(leg_id: str, params: dict):
    status, data, ms = chat(params)
    if status != 200:
        return {
            "status": status,
            "reasoned": None,
            "rc_len": 0,
            "r_tokens": 0,
            "fields": [],
            "note": f"HTTP {status}: {str(data)[:200]}",
        }
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    usage = data.get("usage") or {}
    reasoned, rc_len, r_tokens, present = reasoning_signals(msg, usage)
    return {
        "status": status,
        "reasoned": reasoned,
        "rc_len": rc_len,
        "r_tokens": r_tokens,
        "fields": present,
        "note": f"{ms * 1000:.0f}ms finish={((data.get('choices') or [{}])[0].get('finish_reason'))}",
    }


def main():
    wanted = sys.argv[1:] or [leg[0] for leg in LEGS]
    print(f"target model : {MODEL}  (effort for reasoning-on legs: {EFFORT})")
    print(f"prompt       : {PROMPT!r}")
    print("payload rule: reasoning object with explicit keys (enabled + effort)")
    print("             -- no root-level spellings (see git history for the "
          "findings that removed them)")
    print(f"{'leg':<14}{'status':<7}{'reasoned':<9}{'exp':<9}{'rc_len':<7}"
          f"{'rtoks':<6}{'fields':<60}note")
    results = {}
    for leg_id, params, expected, desc in LEGS:
        if leg_id not in wanted:
            continue
        res = run_leg(leg_id, params)
        results[leg_id] = (res, expected)
        exp_str = "yes" if expected else "no"
        verdict = "OK" if res["status"] == 200 and (
            res["reasoned"] is None or res["reasoned"] == expected
        ) else "UNEXPECTED"
        print(
            f"{leg_id:<14}{res['status']:<7}"
            f"{('yes' if res['reasoned'] else 'no'):<9}{exp_str:<9}"
            f"{res['rc_len']:<7}{res['r_tokens']:<6}"
            f"{','.join(res['fields'])[:58]:<60}{verdict} | {res['note']}"
        )
    print("\n=== verdict ===")
    for leg_id, _, expected, _ in LEGS:
        if leg_id not in results:
            continue
        res, _ = results[leg_id]
        exp_str = "reasoned" if expected else "no reasoning"
        if res["status"] != 200:
            state = f"NOT TOLERATED (HTTP {res['status']})"
        elif res["reasoned"] == expected:
            state = f"as expected ({exp_str})"
        else:
            state = f"UNEXPECTED: expected {exp_str}, got {'reasoned' if res['reasoned'] else 'no reasoning'}"
        print(f"  {leg_id:<14} {state}")


if __name__ == "__main__":
    main()
