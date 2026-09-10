#!/usr/bin/env python3
"""Probe 3 — reasoning response-field drift monitor (LiteLLM -> provider).

Context
-------
Pi's serializer replays stored reasoning under the field name recorded at
stream time (its thinkingSignature). For the replay to stay in the native
field — `reasoning_content`, the one the native route requires and OR
accepts as an alias — the response must deliver the reasoning under
`reasoning_content`. Measured 2026-09-09: this route (OR -> Baidu fp8 via
the gateway) streams reasoning under `reasoning_content`, with no canonical
OR `reasoning` field, so pi stores the native signature after OR-served
turns and the reasoning_route_adapter's message handling stays moot on the
OR route. This probe watches for drift: if the gateway/OR start delivering
the reasoning under the canonical `reasoning` field instead, pi would store
a `reasoning` signature and replay under the wrong field on the next
request — native tolerates the stray field without a 4xx but drops its
content (ad-hoc check 2026-09-09, n=1; see probes/README.md Results).

Contract expectation
--------------------
The reasoning payload arrives under `reasoning_content` (non-stream message
and stream deltas). UNEXPECTED = the canonical `reasoning` field carries the
reasoning while `reasoning_content` is absent/empty in the same response.

Constraints
-----------
- API key from env only (LITELLM_KEY or LITELLM_MASTER_KEY); never hardcoded.
- Reasoning at effort low (credit budget). LITELLM_EFFORT overrides.
- One provider per run. Default target: openrouter/deepseek-v4-flash
  (LiteLLM alias -> OR deepseek-v4-flash-0731 -> Baidu fp8). Native
  baseline: LITELLM_MODEL=deepseek/deepseek-v4-flash (native also delivers
  reasoning_content).
- Two legs per run: `nostream` (message fields) and `stream` (delta fields,
  what pi actually parses). Cost: 2 short requests, effort low => well
  under one cent on the OR route.
- Requests are tagged for the gateway logs via the spend-logs metadata
  header (same scheme as the client models.json headers).

Usage
-----
    .venv/bin/python reasoning_route_adapter/probes/reasoning_response_field.py [nostream|stream]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://litellm.private")
MODEL = os.environ.get("LITELLM_MODEL", "openrouter/deepseek-v4-flash")
EFFORT = os.environ.get("LITELLM_EFFORT", "low")
MAX_TOKENS = 256
PROMPT = "Which number is larger, 9.11 or 9.8? Reply in one word."

REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


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


def _headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {KEY}",
    }
    user = os.environ.get("X_LITELLM_USER_NAME")
    if user:
        headers["x-litellm-user-name"] = user
    headers["x-litellm-spend-logs-metadata"] = os.environ.get(
        "LITELLM_SPEND_META",
        json.dumps({"tool": "probe", "client_host": "probe3"}),
    )
    return headers


def _body(stream: bool) -> dict:
    # Reasoning control: the OR object with explicit keys (repo payload
    # rule). The response-field contract is provider-side, so any control
    # that enables reasoning at low effort is fine.
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "reasoning": {"enabled": True, "effort": EFFORT},
        "max_tokens": MAX_TOKENS,
        "stream": stream,
    }


def _nonempty(fields: dict) -> list:
    """[(field, True)] for fields carrying non-empty reasoning text."""
    return [f for f in REASONING_FIELDS
            if isinstance(fields.get(f), str) and fields[f].strip()]


def _verdict(fields: dict) -> str:
    """OK = reasoning under reasoning_content. UNEXPECTED = canonical
    `reasoning` carries it while reasoning_content is absent/empty."""
    carrying = _nonempty(fields)
    if not carrying:
        return "no reasoning fields"
    if "reasoning" in carrying and "reasoning_content" not in carrying:
        return "UNEXPECTED: reasoning WITHOUT reasoning_content"
    return "OK"


def run_nostream():
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(_body(stream=False)).encode(),
        headers=_headers(),
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"nostream: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}")
        return None
    ms = (time.time() - t0) * 1000
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    fields = {f: msg.get(f) for f in REASONING_FIELDS}
    psf = msg.get("provider_specific_fields") or {}
    for f in REASONING_FIELDS:
        if f in psf and isinstance(psf[f], str):
            fields.setdefault(f, psf[f])
    r_tokens = ((data.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    print(f"nostream: status=200 {ms:.0f}ms msg_keys={sorted(msg.keys())} "
          f"verdict={_verdict(fields)} reasoning_tokens={r_tokens}")
    for f in REASONING_FIELDS:
        v = fields.get(f)
        if isinstance(v, str) and v:
            print(f"          {f}: {len(v)} chars, head={v[:60]!r}")
    return _verdict(fields)


def run_stream():
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(_body(stream=True)).encode(),
        headers=_headers(),
        method="POST",
    )
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as exc:
        print(f"stream: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}")
        return None
    delta_fields = {f: "" for f in REASONING_FIELDS}
    delta_keys = set()
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
            delta_keys.update(delta.keys())
            for f in REASONING_FIELDS:
                v = delta.get(f)
                if isinstance(v, str):
                    delta_fields[f] += v
    ms = (time.time() - t0) * 1000
    print(f"stream: status=200 {ms:.0f}ms delta_keys={sorted(delta_keys)} "
          f"verdict={_verdict(delta_fields)}")
    for f in REASONING_FIELDS:
        if delta_fields[f]:
            print(f"        {f}: {len(delta_fields[f])} chars, head={delta_fields[f][:60]!r}")
    return _verdict(delta_fields)


def main():
    wanted = sys.argv[1:] or ["nostream", "stream"]
    print(f"target model : {MODEL}  (effort: {EFFORT})")
    print(f"prompt       : {PROMPT!r}")
    verdicts = []
    if "nostream" in wanted:
        verdicts.append(run_nostream())
    if "stream" in wanted:
        verdicts.append(run_stream())
    bad = [v for v in verdicts if v and v.startswith("UNEXPECTED")]
    print("\n=== verdict ===")
    print("drift detected: reasoning delivered under `reasoning` without "
          "`reasoning_content`" if bad else
          "contract holds: reasoning delivered under `reasoning_content`")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
