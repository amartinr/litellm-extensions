# Probes — reasoning-format contract for DeepSeek via OpenRouter

Context: clients of this gateway (the Open WebUI `agent_loop_guard` pipe, the
pi `pi-deepseek-reasoning-chain-fix` extension) build requests in DeepSeek's
native reasoning vocabulary. When `time_router` sends alias traffic to
OpenRouter during peak windows, those requests reach OpenRouter, which
normalizes reasoning through its own interface. These probes verify, against
the live endpoint, that requests conform to the OR contract for DeepSeek and
monitor for drift.

## Contract under test (deepseek/deepseek-v4-flash-0731 via OR)

OR exposes per-model reasoning metadata in `GET /api/v1/models` (public, no
key):

```json
"reasoning": {
  "mandatory": false,
  "default_enabled": true,
  "supported_efforts": ["max", "high", "low"],
  "default_effort": "high"
}
```

- `supported_efforts` == DeepSeek's native 3 levels (low/high/max); OR
  exposes no extra levels for this model. DeepSeek's own docs collapse the
  wider vocabulary: `medium`→`high`, `xhigh`→`high`.
- `thinking` is not part of the OR API and is ignored on this route — the
  DeepSeek-native kill-switch `thinking:{type:disabled}` does not disable
  reasoning (verified: the leg still reasoned; confirmed after a gateway
  restart).
- OR-native disable `reasoning:{effort:"none"}` works (`mandatory: false`).
  Root `reasoning_effort:"none"` does NOT disable on this route (probed: 2
  runs, still reasoned 53-55 tokens) — "none" is not in the model's
  supported set nor in OpenAI's own `reasoning_effort` vocabulary.
- Spelling asymmetry (verified): root `reasoning_effort:"low"` behaves like
  native low (modest), while the object `reasoning:{effort:"low"}` spent the
  full 256-token budget on a trivial prompt (`finish: length`).
- Tool-call histories: OR accepts `reasoning_content` on assistant messages
  as an alias for its `reasoning` field (docs, best-practices/
  reasoning-tokens); missing field is tolerated here (no 4xx) and real-text
  replay gives the richest continuation reasoning.

## Rules

1. **No hardcoded API key.** Key comes from `LITELLM_KEY` or
   `LITELLM_MASTER_KEY` (repo `.env.example` name) — the scripts refuse to run
   without it.
2. **Reasoning on Baidu is always exercised at effort `low`** (credit budget).
   Override only for a native-DeepSeek comparison run: `LITELLM_EFFORT=...`.
3. **No cross-provider histories.** Every test starts and ends with ONE
   provider. Default target: `openrouter/deepseek-v4-flash` (LiteLLM alias →
   OR `deepseek-v4-flash-0731` → Baidu fp8, `provider.order: ["baidu/fp8"]`,
   no fallbacks). The native route (`deepseek/deepseek-v4-flash`, direct
   API) is a comparison baseline only, via `LITELLM_MODEL` — a comparison
   run still uses a single provider throughout.
4. **Small, precise probes.** Short prompts, `max_tokens` capped (256), no
   streaming. Full default run ≈ 14 requests ≈ well under one cent on the
   OpenRouter route.

## Setup

```bash
python3 -m venv .venv              # stdlib only — no pip installs needed
export LITELLM_MASTER_KEY=sk-...   # or LITELLM_KEY (never written to disk)
```

## Probe 1 — `reasoning_format_tolerance.py`

Six single-turn legs, one request each. Each leg encodes a contract
expectation; `UNEXPECTED` means the endpoint drifted from it.

| leg | extra body params | expected reasoning | role |
|---|---|---|---|
| `none` | *(none)* | yes | OR metadata default (enabled, effort high) |
| `root_low` | `reasoning_effort:"low"` | yes | recommended ON spelling (native vocab) |
| `obj_none` | `reasoning:{effort:"none"}` | no | OR-native OFF (verified) |
| `thinking_off` | `thinking:{type:"disabled"}` | yes | regression: DeepSeek kill-switch ignored on OR |
| `obj_low` | `reasoning:{effort:"low"}` | yes | monitor: object-spelling full-budget burn |

```bash
.venv/bin/python probes/reasoning_format_tolerance.py            # OR → Baidu
# comparison baseline (same legs, native route, single provider):
LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/reasoning_format_tolerance.py
# rerun only some legs (economize):
.venv/bin/python probes/reasoning_format_tolerance.py root_none thinking_off
```

## Probe 2 — `tool_replay_tolerance.py`

Tool-call continuation trio, one provider per run. Two-step tool task (turn 1
calls `get_date`; the continuation must compute "tomorrow" and call
`get_weather`), so the continuation has something to reason about — a
single-step relay produces no reasoning at effort `low` in any leg (v1 flaw,
see git history). The replayed assistant message differs only in its
`reasoning_content`: A: the REAL text, B: NO field (how Open WebUI rebuilds
assistant history), C: the `" "` placeholder (what both fixes force).

Measures per leg over N rounds (default 2): HTTP status (4xx = format
rejection), continuation reasoning (length + usage reasoning tokens), and
whether the chain continues to `get_weather`. Transient 429/5xx are retried
with backoff and reported separately — they are provider availability issues,
not format verdicts.

```bash
.venv/bin/python probes/tool_replay_tolerance.py [rounds=2]                 # OR → Baidu
LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/tool_replay_tolerance.py [rounds=2]
```

## Results (2026-09-08, target: OpenRouter → Baidu fp8, effort low)

### Probe 1 — request-format matrix

- All spellings returned HTTP 200. No 4xx format rejection, no 426.
- Reasoning engages by default (`none`) and responses are normalized by
  LiteLLM to `reasoning_content` + `provider_specific_fields.reasoning`
  (readable by both clients).
- `thinking:{type:"disabled"}` NOT honored (49 / 27 reasoning tokens in two
  runs, confirmed after a gateway restart) — DeepSeek kill-switch lost on
  this route; only OR-native OFF works.
- `reasoning:{effort:"none"}` honored: 0 reasoning tokens. Root
  `reasoning_effort:"none"` is NOT an OFF mechanism here (probed, 2 runs:
  still reasoned 53-55 tokens).
- Spelling asymmetry at the same nominal `low`: root `reasoning_effort`
  → 64 reasoning tokens; object `reasoning:{effort:"low"}` → full 256-token
  budget (`finish: length`, no answer).

### Probe 2 — tool-call continuation trio (two-step task, 7 clean rounds total)

- All legs HTTP 200: a missing `reasoning_content` on the tool-call
  assistant message is TOLERATED on this route (no 4xx). Raw DeepSeek
  rejects it (400); LiteLLM-native injects a placeholder + warning.
- The chain continues to `get_weather` in every leg (7/7 per leg).
- Continuation reasoning richness ranks A > B ≈ C:

  | leg (replayed assistant reasoning_content) | reasoned | avg rc len |
  |---|---|---|
  | A_real (real text) | 7/7 | 95 |
  | B_missing (no field — Open WebUI rebuild) | 7/7 | 59 |
  | C_space (" " placeholder — client fallback) | 6/7 | 47–74 |

  Real-text replay gives the richest, most consistent continuation
  reasoning; the " " placeholder occasionally yields zero reasoning on a
  continuation (1/4 in one run).

### Transients

Baidu rate-limited 2 of ~25 probe requests (one 429 mid-run, one earlier),
all outside peak windows and at minimal volume. The route is tolerant of
format variations; its constraint is intermittent 429s, consistent with the
gateway's failure metrics for this provider.
