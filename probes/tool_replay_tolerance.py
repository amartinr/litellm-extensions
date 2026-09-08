#!/usr/bin/env python3
"""Probe 2 — tool-call continuation replay trio, OR->Baidu vs native.

Purpose
-------
OR contract for DeepSeek tool-calling histories (docs, best-practices/
reasoning-tokens): pass the previous reasoning back in assistant messages
(message.reasoning, or `reasoning_content` — an accepted alias) so the model
continues its chain after tool results. On raw DeepSeek a missing field is
HTTP 400; through this gateway the native route injects a placeholder +
warning. The clients in front of this gateway replay the real reasoning
text, a " " placeholder, or nothing (Open WebUI rebuild). This probe
measures, on the OpenRouter->Baidu route, whether the field is required
(4xx tolerance) and whether replay quality changes continuation reasoning.

Design (v2)
-----------
The continuation must have something to reason about. v1 asked it to relay a
single tool result — no reasoning at effort low in any leg, so legs could not
discriminate. v2 mirrors the native-route A/B (open-webui-extensions
probes/litellm/03_replay_ab.py): a two-step tool task where the continuation
must compute "tomorrow" from the get_date result and call get_weather:

    user: "What will the weather be in Madrid tomorrow? Use the tools."
    turn 1: model reasons + calls get_date
    continuation (legs differ only in the replayed assistant's
    reasoning_content): model reasons again, computes tomorrow, calls
    get_weather, answers.

Established so far on this route (2026-09-08): missing reasoning_content is
tolerated (no 4xx — OR does not enforce DeepSeek's presence validation) and
real-text replay gives the richest continuation reasoning.

Constraints
-----------
- API key from env only (LITELLM_KEY or LITELLM_MASTER_KEY); refuses to run
  without it. Never hardcoded.
- Reasoning exercised at effort "low" (credit budget); LITELLM_EFFORT
  override for a deliberate comparison run.
- One provider per run: every call in a round uses the same model. Default =
  openrouter/deepseek-v4-flash (OR -> Baidu fp8). Native comparison:
  LITELLM_MODEL=deepseek/deepseek-v4-flash. No cross-provider histories.
- rounds default 2 (each clean round = 1 tool-call request + 3 continuation
  requests = 4 requests); pass a number as argv[1] to change.

Usage
-----
    .venv/bin/python probes/tool_replay_tolerance.py [rounds=2]
    LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/tool_replay_tolerance.py [rounds=2]

Cost
----
rounds=2 => 8 requests, max_tokens=256, effort low => well under $0.001 on
the OpenRouter route.
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

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_date",
            "description": "Get the current date (ISO).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather for a city on a date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "date": {"type": "string", "description": "ISO date"},
                },
                "required": ["city", "date"],
            },
        },
    },
]

USER_MSG = "What will the weather be in Madrid tomorrow? Use the tools."


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


def _is_retryable(status: int) -> bool:
    """429 (rate limit) and 5xx are transient — retry with backoff.
    4xx format rejections (400/422/426) are the tolerance signal and never
    retried."""
    return status == 429 or status >= 500


def _post(body: dict):
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


def chat(messages):
    """POST one non-stream chat completion with tools + effort low.
    Retries 429/5xx twice with backoff (rate limits are the provider's
    business, not the probe's verdict). Returns (status, body, ms, retries)."""
    body = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "reasoning": {"enabled": True, "effort": EFFORT},
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    retries = 0
    for attempt in range(3):
        status, payload, ms = _post(body)
        if _is_retryable(status) and attempt < 2:
            retries += 1
            time.sleep(3 * (attempt + 1))
            continue
        return status, payload, ms, retries
    return status, payload, ms, retries


def reasoning_len(msg: dict) -> int:
    """Length of the reasoning the MODEL produced (any normalized field)."""
    rc = msg.get("reasoning_content")
    if isinstance(rc, str) and rc:
        return len(rc)
    rs = msg.get("reasoning")
    if isinstance(rs, str) and rs:
        return len(rs)
    psf = msg.get("provider_specific_fields") or {}
    p_rs = psf.get("reasoning")
    if isinstance(p_rs, str) and p_rs:
        return len(p_rs)
    return 0


def reasoning_tokens(data: dict) -> int:
    det = ((data.get("usage") or {}).get("completion_tokens_details")) or {}
    return int(det.get("reasoning_tokens") or 0)


def one_round():
    """Turn 1 (get_date) then three single-provider continuation legs.

    All calls in this round use the SAME model — never a mix of providers.
    If turn 1 already called get_weather (parallel tool calling), there is
    nothing left for the continuation to reason about: the round is marked
    degraded and skipped (no continuation calls are made — saves credit).
    """
    status, data, ms, retries = chat([{"role": "user", "content": USER_MSG}])
    if status != 200:
        raise RuntimeError(f"turn1 HTTP {status}: {str(data)[:300]}")
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError("model did not call a tool in turn 1")
    names = [tc["function"]["name"] for tc in tool_calls]
    if "get_weather" in names:
        return {"degraded": True, "names": names}
    tc = tool_calls[0]
    real_rc = msg.get("reasoning_content")
    if not isinstance(real_rc, str):
        real_rc = ""

    # Continuation history: user, assistant(get_date call), tool result.
    # The base assistant has no reasoning_content field = leg B (missing).
    history = [
        {"role": "user", "content": USER_MSG},
        {
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": '{"date": "2026-09-08"}',
        },
    ]

    # Leg A: replay the REAL reasoning text; B: leave missing (Open WebUI
    # rebuild); C: force the " " placeholder (pipe / pi extension fallback).
    legs = {
        "A_real": json.loads(json.dumps(history)),
        "B_missing": json.loads(json.dumps(history)),
        "C_space": json.loads(json.dumps(history)),
    }
    legs["A_real"][1]["reasoning_content"] = real_rc or " "
    legs["C_space"][1]["reasoning_content"] = " "

    out = {"degraded": False, "names": names}
    for tag, messages in legs.items():
        status, data, dt, retries = chat(messages)
        if status != 200:
            out[tag] = {"status": status, "rc_len": -1, "retries": retries,
                        "error": str(data)[:200]}
            continue
        cont = (data.get("choices") or [{}])[0].get("message") or {}
        next_tools = [
            t["function"]["name"] for t in (cont.get("tool_calls") or [])
        ]
        out[tag] = {
            "status": status,
            "rc_len": reasoning_len(cont),
            "rtoks": reasoning_tokens(data),
            "next_tools": next_tools,
            "retries": retries,
            "ms": dt,
        }
    return out


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"target model : {MODEL}  (effort: {EFFORT}, rounds: {rounds})")
    print("task: 2-step tool chain (get_date -> continuation -> get_weather)")
    print("single provider per round: yes — no cross-provider histories")
    agg = {
        tag: {"ok": 0, "reasoned": 0, "rc_tot": 0, "rtoks_tot": 0,
              "weather_calls": 0, "format_err": 0, "transient": 0}
        for tag in ("A_real", "B_missing", "C_space")
    }
    valid = 0
    for r in range(1, rounds + 1):
        try:
            res = one_round()
        except Exception as exc:  # turn-1 failure aborts the round cleanly
            print(f"round {r}: SKIPPED ({exc})")
            continue
        if res["degraded"]:
            print(f"round {r}: DEGRADED (turn 1 called {res['names']} in "
                  f"parallel; nothing left to reason about) — skipped")
            continue
        valid += 1
        for tag in ("A_real", "B_missing", "C_space"):
            rr = res[tag]
            if rr["status"] != 200:
                if _is_retryable(rr["status"]):
                    agg[tag]["transient"] += 1
                    print(f"round {r} {tag}: transient HTTP {rr['status']} "
                          f"after {rr['retries']} retries — {rr['error']}")
                else:
                    agg[tag]["format_err"] += 1
                    print(f"round {r} {tag}: FORMAT REJECTION HTTP "
                          f"{rr['status']} — {rr['error']}")
                continue
            agg[tag]["ok"] += 1
            reasoned = rr["rc_len"] > 0 or rr["rtoks"] > 0
            if reasoned:
                agg[tag]["reasoned"] += 1
            agg[tag]["rc_tot"] += rr["rc_len"]
            agg[tag]["rtoks_tot"] += rr["rtoks"]
            if "get_weather" in rr["next_tools"]:
                agg[tag]["weather_calls"] += 1
            print(
                f"round {r} {tag}: 200 reasoned={reasoned} rc_len={rr['rc_len']} "
                f"rtoks={rr['rtoks']} next={rr['next_tools'] or '(final answer)'} "
                f"({rr['ms'] * 1000:.0f}ms)"
            )
    print(f"\n=== verdict (per leg, {valid} clean rounds, effort={EFFORT}) ===")
    for tag in ("A_real", "B_missing", "C_space"):
        a = agg[tag]
        avg_rc = f"{a['rc_tot'] / a['ok']:.1f}" if a["ok"] else "-"
        avg_rt = f"{a['rtoks_tot'] / a['ok']:.1f}" if a["ok"] else "-"
        print(
            f"  {tag:<10} ok={a['ok']} reasoned={a['reasoned']}/{a['ok']} "
            f"avg_rc_len={avg_rc} avg_rtoks={avg_rt} "
            f"weather_calls={a['weather_calls']}/{a['ok']} "
            f"format_err={a['format_err']} transient={a['transient']}"
        )
    b = agg["B_missing"]
    if b["format_err"]:
        print("  => B_missing (no reasoning_content) is FORMAT-REJECTED on "
              "this route (HTTP 4xx) — OpenRouter->Baidu enforces the "
              "DeepSeek presence contract.")
    elif b["ok"]:
        print("  => missing reasoning_content is TOLERATED on this route "
              "(no 4xx format rejection). Continuation reasoning above shows "
              "whether replay quality matters. Transient errors (429/5xx) "
              "are provider rate/availability issues, not format verdicts.")


if __name__ == "__main__":
    main()
