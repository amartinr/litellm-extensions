#!/usr/bin/env python3
"""Probe 2 — tool-call continuation replay trio, OR->Baidu vs native.

WHY
---
The DeepSeek API contract requires `reasoning_content` on EVERY assistant
message once a history contains tool calls (missing field = HTTP 400 on the
raw API; LiteLLM's transformation injects a " " placeholder + warning on the
native route). The clients in front of this gateway either replay the real
reasoning text, replay a " " placeholder, or (Open WebUI rebuild) replay
nothing. All previous A/B evidence was collected on the NATIVE
(deepseek/deepseek-v4-flash) route only. This probe runs the same trio
against the OpenRouter->Baidu route: is the field required there too? Is a
missing field tolerated? Does a " " placeholder still allow reasoning?

RULES (user constraints)
------------------------
- API key NEVER hardcoded: read LITELLM_KEY or LITELLM_MASTER_KEY.
- Reasoning exercised at effort "low" (credit budget; LITELLM_EFFORT override
  only for comparison runs).
- One provider per run: every pair of calls in a round uses the SAME model.
  Default = openrouter/deepseek-v4-flash (OR -> Baidu fp8). Native comparison:
  LITELLM_MODEL=deepseek/deepseek-v4-flash. NO cross-provider histories.
- rounds default 2 (each round = 1 tool-call request + 3 continuation
  requests = 4 requests); pass a number as argv[1] to change.

USAGE
-----
    .venv/bin/python probes/tool_replay_tolerance.py [rounds=2]
    LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/tool_replay_tolerance.py [rounds=2]

COST
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
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

USER_MSG = "What is the weather in Madrid? Use the tool."


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


def chat(messages):
    """POST one non-stream chat completion with tools + effort low."""
    body = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "reasoning_effort": EFFORT,
        "max_tokens": MAX_TOKENS,
        "stream": False,
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


def message_reasoning(msg: dict) -> int:
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


def one_round():
    """Turn 1 (real tool call) then three single-provider continuation legs.

    All four calls in this round use the SAME model — never a mix of
    providers inside a round or across rounds of a run.
    """
    # Turn 1: model reasons and calls the tool (produces the real assistant).
    status, data, ms = chat([{"role": "user", "content": USER_MSG}])
    if status != 200:
        raise RuntimeError(f"turn1 HTTP {status}: {str(data)[:300]}")
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError("model did not call the tool in turn 1")
    tc = tool_calls[0]
    real_rc = msg.get("reasoning_content") or ""
    if not isinstance(real_rc, str):
        real_rc = ""

    # Base continuation history: user, assistant(tool_call), tool result.
    assistant = {
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
    }
    history = [
        {"role": "user", "content": USER_MSG},
        assistant,
        {
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": '{"temp": 28, "city": "Madrid", "sky": "sunny"}',
        },
    ]

    # The base assistant (as built above) has NO reasoning_content field —
    # that is exactly leg B (how Open WebUI rebuilds assistant messages), so
    # B_missing needs no mutation. Leg A replays the REAL reasoning text;
    # leg C forces the " " placeholder (what the pipe / pi extension send
    # when no real text is available).
    legs = {
        "A_real": json.loads(json.dumps(history)),
        "B_missing": json.loads(json.dumps(history)),
        "C_space": json.loads(json.dumps(history)),
    }
    legs["A_real"][1]["reasoning_content"] = real_rc or " "
    legs["C_space"][1]["reasoning_content"] = " "

    out = {}
    for tag, messages in legs.items():
        status, data, dt = chat(messages)
        if status != 200:
            out[tag] = {"status": status, "rc_len": -1, "error": str(data)[:200]}
            continue
        cont = (data.get("choices") or [{}])[0].get("message") or {}
        out[tag] = {
            "status": status,
            "rc_len": message_reasoning(cont),
            "error": "",
            "ms": dt,
        }
    return out


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"target model : {MODEL}  (effort: {EFFORT}, rounds: {rounds})")
    print(f"single provider per round: yes — no cross-provider histories")
    agg = {tag: {"ok": 0, "reasoned": 0, "err": 0, "rc_tot": 0} for tag in ("A_real", "B_missing", "C_space")}
    for r in range(1, rounds + 1):
        try:
            res = one_round()
        except Exception as exc:  # turn-1 failure aborts the round cleanly
            print(f"round {r}: SKIPPED ({exc})")
            continue
        for tag, r_ in res.items():
            if r_["status"] != 200:
                agg[tag]["err"] += 1
                print(f"round {r} {tag}: HTTP {r_['status']} — {r_['error']}")
            else:
                agg[tag]["ok"] += 1
                reasoned = r_["rc_len"] > 0
                if reasoned:
                    agg[tag]["reasoned"] += 1
                agg[tag]["rc_tot"] += r_["rc_len"]
                print(
                    f"round {r} {tag}: 200 reasoned={reasoned} rc_len={r_['rc_len']} "
                    f"({r_['ms'] * 1000:.0f}ms)"
                )
    print("\n=== verdict (per leg) ===")
    for tag in ("A_real", "B_missing", "C_space"):
        a = agg[tag]
        avg = f"{a['rc_tot'] / a['ok']:.1f}" if a["ok"] else "-"
        print(
            f"  {tag:<10} status_ok={a['ok']}/{a['ok'] + a['err']} "
            f"reasoned={a['reasoned']}/{a['ok']} avg_rc_len={avg}"
        )
    b = agg["B_missing"]
    if b["err"] > 0:
        print("  => B_missing (no reasoning_content) is NOT tolerated: "
              "OR->Baidu enforces the DeepSeek presence contract.")
    elif b["ok"] > 0 and b["reasoned"] < agg["A_real"]["reasoned"]:
        print("  => B_missing degrades continuation reasoning vs real text.")
    else:
        print("  => B_missing tolerated without measurable reasoning loss "
              "(at low effort) — further check warranted only if A/B differ "
              "at higher effort.")


if __name__ == "__main__":
    main()
