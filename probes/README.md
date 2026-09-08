# Probes — tolerance of the OpenRouter→Baidu route to DeepSeek-native reasoning formats

When the `time_router` hook sends alias traffic (`litellm/deepseek-v4-flash`) to
OpenRouter during peak windows, the outbound requests are built by clients that
speak DeepSeek's NATIVE reasoning vocabulary (the Open WebUI `agent_loop_guard`
pipe and the pi `pi-deepseek-reasoning-chain-fix` extension force/replay
`reasoning_content` on assistant messages and may carry `thinking` /
`reasoning_effort` at the root). OpenRouter normalizes reasoning through its own
`reasoning` object. These probes measure, against the REAL endpoint, which
request shapes the OpenRouter→Baidu (fp8) deployment of
`deepseek-v4-flash` tolerates and what actually happens to reasoning.

They answer the question behind the session: **does the DeepSeek-native format
sent by our clients degrade reasoning (or the kill-switch) when the request is
routed through OpenRouter in peak hours?**

## Rules (hard constraints from the user)

1. **No hardcoded API key.** Key comes from `LITELLM_KEY` or
   `LITELLM_MASTER_KEY` (repo `.env.example` name) — the scripts refuse to run
   without it.
2. **Reasoning on Baidu is always exercised at effort `low`** (credit budget).
   Override only for a native-DeepSeek comparison run: `LITELLM_EFFORT=...`.
3. **No cross-provider histories, ever.** Every test starts and ends with ONE
   provider. Default target: `openrouter/deepseek-v4-flash` (OR → Baidu fp8,
   `provider.order: ["baidu/fp8"]`, no fallbacks). The native route
   (`deepseek/deepseek-v4-flash`, direct API) is used ONLY as a comparison
   baseline via `LITELLM_MODEL` — a comparison run still uses a single
   provider for the whole run.
4. **Small, precise probes.** Short prompts, `max_tokens` capped (256), no
   streaming. Full default run ≈ 15 requests ≈ well under one cent on the
   OpenRouter route.

## Setup (this repo)

```bash
python3 -m venv .venv              # stdlib only — no pip installs needed
export LITELLM_MASTER_KEY=sk-...   # or LITELLM_KEY (never written to disk)
```

## Probe 1 — `reasoning_format_tolerance.py`

Single-turn matrix: one short prompt, seven request-parameter shapes, one
request each. Measures HTTP tolerance and whether reasoning actually engages
(and can be switched OFF).

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

Tool-call continuation trio (the shape the pipe/pi extension fight over), all
on one provider per run: turn 1 makes a real tool call; the continuation then
replays the assistant message in three variants — A: with the REAL
`reasoning_content` text, B: WITHOUT the field (how Open WebUI rebuilds
assistant history), C: with the `" "` placeholder (what both fixes force).
`tools` and `reasoning_effort: low` are constant across legs and calls.

Measures per leg over N rounds (default 2): HTTP status (tolerance — does
OR→Baidu 400 on a missing field like raw DeepSeek does?), and whether the
continuation still reasons.

```bash
.venv/bin/python probes/tool_replay_tolerance.py [rounds=2]                 # OR → Baidu
LITELLM_MODEL=deepseek/deepseek-v4-flash .venv/bin/python probes/tool_replay_tolerance.py [rounds=2]
```

## Interpreting the verdicts

- A leg marked **UNEXPECTED** means the endpoint behaved differently from the
  DeepSeek native contract — that difference is exactly the "impact during
  peak hours" we are after. The two most important checks:
  - leg 5 (`native_off`): if it still reasons, the DeepSeek kill-switch is
    silently lost through OR→Baidu (users who disabled thinking pay for
    reasoning anyway during peak).
  - probe 2 leg B: if it 400s, OpenRouter→Baidu enforces the same presence
    contract as raw DeepSeek, and Open WebUI's rebuild (which strips the
    field) would break on the OR route — the pipe's forcing is what saves it.
- Response fields (`reasoning_content` / `reasoning` /
  `provider_specific_fields`) are logged per request: LiteLLM normalizes the
  OR response back to DeepSeek shape; a change there (e.g. reasoning only in
  `provider_specific_fields`) would affect what both clients can store/replay.
