# DEPLOYMENT — client-side activation for `reasoning_route_adapter`

This document covers the **client-side** changes that the adapter's
peak-hour fix depends on. It is specific to this reference deployment
(pi coding agent in front of a DB-less LiteLLM gateway, see `DESIGN.md` §1).
The gateway-side hook itself is generic; only this document is
deployment-specific.

Status: activation notes, 2026-09-09. The gateway adapter is implemented and
offline-tested; the client delta and the gateway change land **together**.

## Why a client-side change is needed

The public alias `litellm/deepseek-v4-flash` is routed by `time_router` to
one of two upstreams with different reasoning dialects:

| Route | Reasoning control it honors |
|---|---|
| native DeepSeek (off-peak) | `thinking:{type:enabled\|disabled}` + root `reasoning_effort` |
| OpenRouter → Baidu (peak) | object `reasoning:{enabled,effort}`; root `reasoning_effort` |

Because the alias is fictitious and the route is chosen **per request from the
clock**, the client cannot know a priori which dialect it is talking to. The
design resolves this by making the client commit to **one** canonical dialect
(DeepSeek-native — the alias *is* a DeepSeek model) and letting the gateway
adapter normalize to whichever deployment is bound (DESIGN §4.3–§4.5). The
adapter is symmetric, so either dialect could be the canonical client one;
native is chosen because the model identity is DeepSeek.

Without a client-side declaration, pi's `detectCompat` classifies the
`litellm` provider (`baseUrl = gateway`, not `deepseek.com`) as generic
OpenAI (`thinkingFormat: "openai"`). Then:

- reasoning **ON** → root `reasoning_effort` (honored and cheap on both
  routes);
- reasoning **OFF** → pi emits **nothing**, so no kill switch reaches the
  gateway and DeepSeek reasons by default — the user pays for reasoning they
  disabled (silent over-spend on both routes).

pi does not detect the dialect; the `compat` block **declares** it.

## 1. pi `models.json` delta

Current state: provider keyed `litellm` (baseUrl = gateway, `api:
openai-completions`, `compat` only `supportsDeveloperRole:false` +
`supportsReasoningEffort:true`).

Required `compat` delta on every DeepSeek-backed entry (including the
`litellm/deepseek-v4-flash` alias):

```jsonc
"compat": {
  "supportsDeveloperRole": false,
  "supportsReasoningEffort": true,
  "thinkingFormat": "deepseek",   // kill-switch emission
  "maxTokensField": "max_tokens", // native field name
  "requiresReasoningContentOnAssistantMessages": true  // tool-scope "" net
}
```

Effects after the delta:

- **OFF** → `thinking:{type:"disabled"}`: honored natively (off-peak) and
  rescued by the adapter to the OR OFF object (peak, 0 tokens).
- **ON** → `thinking:{type:"enabled"}` is added next to the root
  `reasoning_effort` (the documented native curl form); behavior is
  byte-equivalent on both routes (the adapter drops the redundant `thinking`
  on OR and keeps the cheap root effort).
- `max_tokens` replaces `max_completion_tokens` (the field DeepSeek
  documents).
- Tool scope: pi core forces `reasoning_content` (`""`).

Constraint: `thinkingLevelMap.off` must **not** be `null`, or pi will not emit
the kill switch even with `thinkingFormat:"deepseek"`. Leaving the key absent
is fine (`undefined !== null`).

## 2. Extension dependency — `amartinr/pi-deepseek-reasoning-chain-fix`

The kill switch itself comes from pi **core** via the `compat` delta above;
it does **not** require the extension. The extension is the separate
history-replay layer: DeepSeek requires `reasoning_content` on every assistant
message once the history contains tool calls, and the extension keeps the
real reasoning text chained across those turns.

- Repository: <https://github.com/amartinr/pi-deepseek-reasoning-chain-fix>
- Install: `pi install npm:@amartinr/pi-deepseek-reasoning-chain-fix`
  (or `pi install git:github.com/amartinr/pi-deepseek-reasoning-chain-fix`)
- Scope: it applies only to the model ids listed in
  `~/.pi/agent/extensions/pi-deepseek-reasoning-chain-fix/config.json`
  (exact or case-insensitive prefix match).

The alias `litellm/deepseek-v4-flash` must be covered by that scope
(either listed explicitly or via a `"deepseek/"` prefix). Role after the
`compat` delta:

- wire-compliance refinement (`" "` placeholder vs pi core's `""`), and
- signature restore for resumed/persisted sessions (pi core replays real text
  only when the stored thinking block carries a recognized signature).

Its OR-turn signature normalization is defensive insurance, not active on this
route: the gateway delivers OR reasoning as `reasoning_content` (probe 3,
2026-09-09), so pi already stores the native signature after OR-served turns.
If that ever drifts to the canonical `reasoning`, native tolerates the stray
field without a 4xx but silently drops its content (ad-hoc check 2026-09-09,
n=1) and the extension becomes the active guard.

Coupling summary:

| Layer | Depends on | Nature |
|---|---|---|
| `reasoning_route_adapter` (gateway) | client emits one known dialect | documented contract, not a code dependency |
| `models.json` `compat` delta | — (pi core) | hard requirement for the kill switch |
| `amartinr/pi-deepseek-reasoning-chain-fix` | `models.json` model scope | soft/orthogonal: history replay, not dialect selection |

## 3. Behavioral deltas at deployment

- **Off-peak** (alias → native): the adapter is a no-op for native-dialect
  payloads (native-bound rules only act on an OR `reasoning` object).
  `models.json` delta: reasoning OFF now actually disables reasoning (the
  fix); reasoning ON byte-equivalent; `reasoning_content: ""` vs LiteLLM's
  `" "` placeholder — same blank-chain class.
- **Peak** (alias → OR): reasoning ON → the adapter drops the redundant
  `thinking` and keeps root `reasoning_effort` (cheap on OR) —
  byte-equivalent outcome; reasoning OFF → `kill_switch_rescue` to the OR
  OFF object — 0 tokens (was 47–57 and billed).

> Effort **level** caveat (measured 2026-09-12, LiteLLM v1.99.0): the level
> is not usable through the gateway. On native, LiteLLM discards it
> ([litellm #27439]); on OR it is forwarded but the provider honours it
> erratically. Only the ON/OFF kill switch is reliable. See `DESIGN.md` §1.4
> and `PLAN.md`.

## References

- Gateway behavior, rules and evidence: [`DESIGN.md`](DESIGN.md)
  (§1.2 client contract, §4.3–§4.5 normalization, §6.2 live acceptance).
- Extension: <https://github.com/amartinr/pi-deepseek-reasoning-chain-fix>.
