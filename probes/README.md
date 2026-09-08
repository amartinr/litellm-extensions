# Probes — reasoning-format tolerance of the OpenRouter→Baidu route

Context: clients of this gateway (the Open WebUI `agent_loop_guard` pipe, the
pi `pi-deepseek-reasoning-chain-fix` extension) build requests in DeepSeek's
native reasoning vocabulary (`thinking` / root `reasoning_effort`;
`reasoning_content` on assistant messages). When `time_router` sends alias
traffic to OpenRouter during peak windows, those requests reach OpenRouter,
which normalizes reasoning through its own `reasoning` object. These probes
measure, against the live endpoint, which request shapes the OR→Baidu (fp8)
deployment of `deepseek-v4-flash` tolerates and what happens to reasoning.

## Rules

1. **No hardcoded API key.** Key comes from `LITELLM_KEY` or
   `LITELLM_MASTER_KEY` (repo `.env.example` name) — the scripts refuse to run
   without it.
2. **Reasoning on Baidu is always exercised at effort `low`** (credit budget).
   Override only for a native-DeepSeek comparison run: `LITELLM_EFFORT=...`.
3. **No cross-provider histories.** Every test starts and ends with ONE
   provider. Default target: `openrouter/deepseek-v4-flash` (OR → Baidu fp8,
   `provider.order: ["baidu/fp8"]`, no fallbacks). The native route
   (`deepseek/deepseek-v4-flash`, direct API) is a comparison baseline only,
   via `LITELLM_MODEL` — a comparison run still uses a single provider
   throughout.
4. **Small, precise probes.** Short prompts, `max_tokens` capped (256), no
   streaming. Full default run ≈ 15 requests ≈ well under one cent on the
   OpenRouter route.

## Setup

```bash
python3 -m venv .venv              # stdlib only — no pip installs needed
export LITELLM_MASTER_KEY=sk-...   # or LITELLM_KEY (never written to disk)
```

## Probe 1 — `reasoning_format_tolerance.py`

Single-turn matrix: one short prompt, seven request-parameter shapes, one
request each. Measures HTTP tolerance and whether reasoning engages (and can
be turned off).

| # | leg | extra body params | expected reasoning | question it answers |
|---|---|---|---|---|
| 1 | `none` | *(none)* | yes (DeepSeek default ON) | does OR→Baidu default to thinking like native? |
| 2 | `native_on` | `thinking:{type:enabled}` + `reasoning_effort:low` | yes | DeepSeek-native ON tolerated? |
| 3 | `root_effort` | `reasoning_effort:low` | yes | OpenAI-style root effort (documented OR param)? |
| 4 | `or_reasoning` | `reasoning:{effort:low}` | yes | OR-native control object? |
| 5 | `native_off` | `thinking:{type:disabled}` | **no** | **is the kill-switch honored through OR→Baidu, or lost?** |
| 6 | `or_off` | `reasoning:{effort:none}` | no | OR-native OFF? |
| 7 | `mixed` | all three at once | yes (or 400) | conflicting/redundant formats tolerated? |

```bash
.venv/bin/python probes/reasoning_format_tolerance.py            # OR → Baidu
# comparison baseline (same matrix, native route, single provider):
LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/reasoning_format_tolerance.py
# rerun only some legs (economize):
.venv/bin/python probes/reasoning_format_tolerance.py native_off or_off
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

## Interpreting verdicts

A leg marked **UNEXPECTED** (probe 1) means the endpoint behaved differently
from the DeepSeek native contract. The two most important checks:

- probe 1 leg 5 (`native_off`): if it still reasons, the DeepSeek kill-switch
  is silently lost through OR→Baidu (users who disabled thinking pay for
  reasoning anyway during peak).
- probe 2 leg B: if it 4xxes, OR→Baidu enforces the presence contract like
  raw DeepSeek, and Open WebUI's rebuild (which strips the field) would break
  on the OR route — the pipe's forcing is what prevents that.

Response fields (`reasoning_content` / `reasoning` /
`provider_specific_fields`) are logged per request: LiteLLM normalizes the OR
response back to DeepSeek shape; a change there (e.g. reasoning only in
`provider_specific_fields`) would affect what both clients can store/replay.

## Results (2026-09-08, target: OpenRouter → Baidu fp8, effort low)

### Probe 1 — single-turn format matrix

- All 7 request shapes returned HTTP 200. No 4xx format rejection, no 426.
- Reasoning engages by default (`none`) and with all "on" forms; responses
  are normalized by LiteLLM to `reasoning_content` +
  `provider_specific_fields.reasoning` (readable by both clients).
- `thinking: {"type": "disabled"}` (DeepSeek-native kill-switch) is NOT
  honored on this route: the leg still reasoned (49 / 27 reasoning tokens in
  two runs). Confirmed again after a gateway restart.
- `reasoning: {"effort": "none"}` (OR-native off) is honored: 0 reasoning
  tokens.
- Effort semantics differ by format: `reasoning: {"effort": "low"}` (OR
  object) spent the full 256-token budget on reasoning on a trivial prompt
  (`finish: length`, no answer), while native forms
  (`thinking` + `reasoning_effort: low`) used 47–87 tokens.

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
