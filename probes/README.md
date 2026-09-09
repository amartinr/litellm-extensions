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

Payload rule (user directive): always send explicit key names and values;
never rely on inference or omit keys. The reasoning control is the OR object
with BOTH keys — `enabled` and `effort` — even when redundant
(`{"enabled": false, "effort": "none"}` for OFF). Root-level spellings are
not used.

Rationale for the payload rule (findings of 2026-09-08, probed before the
rule was set; see git history):

- `thinking` is not part of the OR API and is ignored on this route — the
  DeepSeek-native kill-switch `thinking:{type:disabled}` does not disable
  reasoning (still reasoned in two runs, confirmed after a gateway restart).
- Root `reasoning_effort:"none"` does not disable on this route (2 runs,
  still reasoned 53-55 tokens); "none" is not in the model's supported set
  nor in OpenAI's own `reasoning_effort` vocabulary.
- Object-spelling cost: `reasoning:{effort:"low"}` and
  `reasoning:{enabled:true, effort:"low"}` both spent the full 256-token
  budget on a reasoning-trap prompt (`finish: length`) while root
  `reasoning_effort:"low"` was modest — the burn is a property of the object
  spelling on this provider, not of omitted keys.
- `supported_efforts` == DeepSeek's native 3 levels (low/high/max); OR
  exposes no extra levels for this model. DeepSeek's own docs collapse the
  wider vocabulary: `medium`→`high`, `xhigh`→`high`.
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
   streaming. Full default run ≈ 11 requests ≈ well under one cent on the
   OpenRouter route.

## Setup

```bash
python3 -m venv .venv              # stdlib only — no pip installs needed
export LITELLM_MASTER_KEY=sk-...   # or LITELLM_KEY (never written to disk)
```

## Probe 1 — `reasoning_format_tolerance.py`

Three single-turn legs, one request each. Each leg encodes a contract
expectation; `UNEXPECTED` means the endpoint drifted from it.

| leg | extra body params | expected reasoning | role |
|---|---|---|---|
| `none` | *(none)* | yes | OR metadata default (enabled, effort high) |
| `on_low` | `reasoning:{enabled:true, effort:"low"}` | yes | contract form, ON |
| `off` | `reasoning:{enabled:false, effort:"none"}` | no | contract form, OFF (both keys sent) |

```bash
.venv/bin/python probes/reasoning_format_tolerance.py            # OR → Baidu
# comparison baseline (same legs, native route, single provider):
LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/reasoning_format_tolerance.py
# rerun only some legs (economize):
.venv/bin/python probes/reasoning_format_tolerance.py off
```

## Probe 2 — `tool_replay_tolerance.py`

Tool-call continuation trio, one provider per run. Reasoning control is the
explicit-key object (`reasoning:{enabled:true, effort:"low"}`). Two-step tool
task (turn 1 calls `get_date`; the continuation must compute "tomorrow" and
call `get_weather`), so the continuation has something to reason about — a
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

## Probe 3 — `reasoning_response_field.py`

Response-field drift monitor. Pi's serializer replays stored reasoning under
the field name recorded at stream time (its `thinkingSignature`); for the
replay to stay in the native field (`reasoning_content` - required by the
native route, accepted alias by OR), the response must deliver the reasoning
under `reasoning_content`. Two legs per run - `nostream` (message fields)
and `stream` (delta fields, what pi actually parses):

```bash
.venv/bin/python probes/reasoning_response_field.py            # OR → Baidu
LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/reasoning_response_field.py
```

`UNEXPECTED` = the canonical OR `reasoning` field carries the reasoning
while `reasoning_content` is absent/empty in the same response - the drift
that would change pi's stored signature and make it replay the wrong field
on the next request (native tolerates the stray field without a 4xx but
drops its content).

## Results (2026-09-08, target: OpenRouter → Baidu fp8)

### Probe 1 — contract-form matrix (explicit keys)

- `none`: reasons by default (109 reasoning tokens on the trap prompt —
  default effort is high per OR metadata).
- `on_low` (`enabled:true` + `effort:"low"`): reasons, spent the full
  256-token budget (`finish: length`) on the trap prompt. The burn observed
  earlier with the simplified object persists with explicit keys — it is a
  property of the object spelling on this provider, not of omitted keys.
  In tool tasks (probe 2) the same object-low is modest (28-52 tokens).
- `off` (`enabled:false` + `effort:"none"`): 0 reasoning tokens — the
  contract-form OFF works.

### Probe 2 — tool-call continuation trio (two-step task, clean rounds)

- All legs HTTP 200: a missing `reasoning_content` on the tool-call
  assistant message is TOLERATED on this route (no 4xx). Raw DeepSeek
  rejects it (400); LiteLLM-native injects a placeholder + warning.
- The chain continues to `get_weather` in every leg.
- Continuation reasoning richness ranks A > B ≈ C (pooled across runs):

  | leg (replayed assistant reasoning_content) | reasoned | avg rc len |
  |---|---|---|
  | A_real (real text) | ~11/11 | ~93 |
  | B_missing (no field — Open WebUI rebuild) | ~11/11 | ~60 |
  | C_space (" " placeholder — client fallback) | ~10/11 | ~55 |

  Real-text replay gives the richest, most consistent continuation
  reasoning; the " " placeholder occasionally yields zero reasoning on a
  continuation.

### Native-route comparison (deepseek/deepseek-v4-flash, direct API, 2026-09-08)

Same contract-form payload (reasoning object with explicit keys), two runs on
the trap prompt:

- `none`: reasoned (82 / 23 reasoning tokens).
- `on_low` (`enabled:true` + `effort:"low"`): reasoned (51 / 89) —
  indeterminate vs default with n=2 (ranges overlap); no evidence the object
  effort level is honored on the native route, no evidence it is not.
- `off` (`enabled:false` + `effort:"none"`): reasoned in BOTH runs
  (152 / 49) — the contract-form OFF is NOT honored on the native route.

Combined with the OR-route results, OFF is strictly per-route dialect:

| OFF spelling | native DeepSeek | OR → Baidu |
|---|---|---|
| `thinking:{type:"disabled"}` | honored (native kill-switch; 0 deltas verified in the companion repo) | ignored (reasoned 47-57 tokens, 3 runs) |
| `reasoning:{enabled:false, effort:"none"}` | ignored (reasoned 152/49 tokens, 2 runs) | honored (0 tokens, 3 runs) |

No single OFF spelling works on both routes. Level control (effort low via
the object) is honored on OR (with the over-spend noted above) and
indeterminate on native.

### Transients

Baidu rate-limited 2 of ~40 probe requests across the day (one 429 mid-run,
one earlier), all outside peak windows and at minimal volume. The route is
tolerant of format variations; its constraint is intermittent 429s,
consistent with the gateway's failure metrics for this provider.

## Results (2026-09-09)

### Probe 3 - response field (both legs OK, both routes)

- OR → Baidu: non-stream message keys `[content, provider_specific_fields,
  reasoning_content, role]`; stream delta keys `[content, reasoning_content,
  reasoning_details, role]`. The reasoning is delivered under
  `reasoning_content` in both modes - the canonical `reasoning` never
  appears, so pi stores the native signature after OR-served turns (no
  adapter/extension intervention needed on this route).
- Native (`deepseek/deepseek-v4-flash`): `reasoning_content` in both legs
  (the required native field).

Implication: the pi extension's OR-turn signature normalization
(`pi-deepseek-reasoning-chain-fix`) is defensive insurance against drift,
not an active fix on today's route.

### Ad-hoc: native tolerance of replayed-assistant-message shapes (n=1)

Tool-call continuation on the native route through the gateway, three shapes
of the replayed assistant message (the forms pi produces depending on the
stored signature and extension scope):

| shape | status | continuation rc len |
|---|---|---|
| `missing` (no field) | 200 (LiteLLM injects placeholder) | 84 |
| `stray` (`reasoning` real + `reasoning_content` `""`) | 200 | 43 |
| `native` (`reasoning_content` real) | 200 | 104 |

Verdict: the stray canonical `reasoning` field does not 4xx on native via
the gateway, but its content is silently dropped - the weakest continuation.
n=1; only the status is a clean verdict. If the response field ever drifts
to `reasoning` (monitored by probe 3), the replay would degrade this way
until the extension (or a field mapping) restores `reasoning_content`.
