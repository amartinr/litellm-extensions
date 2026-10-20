# DEPLOYMENT — client-side activation for `reasoning_route_adapter`

This document covers the **client-side** changes that the adapter's
peak-hour fix depends on. It is specific to this reference deployment
(pi coding agent in front of a DB-less LiteLLM gateway, see `DESIGN.md` §1).
The gateway-side hook itself is generic; only this document is
deployment-specific.

Status: activation notes, updated 2026-10-20. The gateway adapter is
implemented and offline-tested; the client delta and the gateway change land
**together**.

## Why a client-side change is needed

The public alias `litellm/deepseek-flash` is routed by `time_router` to
one of two upstreams with different reasoning dialects:

| Route | Reasoning control it honors |
|---|---|
| native DeepSeek (off-peak) | `thinking:{type:enabled\|disabled}` + root `reasoning_effort` |
| OpenRouter → DeepInfra (peak) | object `reasoning:{enabled,effort}`; root `reasoning_effort` is rejected by the current model |

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

- reasoning **ON** → root `reasoning_effort`. Native honors it; on OR the
  adapter translates it to the object when the deployment declares
  `reasoning_effort_requires_object: true` (DeepInfra), or leaves it as the
  root key on legacy OR deployments;
- reasoning **OFF** → pi emits **nothing**, so no kill switch reaches the
  gateway and DeepSeek reasons by default — the user pays for reasoning they
  disabled (silent over-spend on both routes).

pi does not detect the dialect; the `compat` block **declares** it.

## 1. pi `models.json` delta

Current state: provider keyed `litellm` (baseUrl = gateway, `api:
openai-completions`, `compat` only `supportsDeveloperRole:false` +
`supportsReasoningEffort:true`).

Required `compat` delta on every DeepSeek-backed entry (including the
`litellm/deepseek-flash` alias):

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
  `reasoning_effort` (the documented native curl form). Native honors both.
  On OR, a deployment with `reasoning_effort_requires_object: true`
  (DeepInfra) translates the effort into `reasoning:{enabled:true, effort}`;
  a legacy OR deployment without the flag keeps the root effort.
- `max_tokens` replaces `max_completion_tokens` (the field DeepSeek
  documents).
- Tool scope: pi core forces `reasoning_content` (`""`); the extension
  upgrades it to `" "` so LiteLLM does not inject its own placeholder and
  warn.

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

The alias `litellm/deepseek-flash` must be covered by that scope
(either listed explicitly or via a `"deepseek/"` prefix). With the current
model ids the extension config must include:

```json
{
  "models": [
    "litellm/deepseek-flash",
    "deepseek/deepseek-flash",
    "deepseek/deepseek-v4-pro"
  ],
  "replayReasoning": true
}
```

Stale ids (`litellm/deepseek-v4-flash`, `deepseek/deepseek-v4-flash`) make the
extension inert for the Flash models. Pi core then sends
`reasoning_content: ""`, which LiteLLM treats as missing and replaces with
`" "`, emitting the warning described in §4.

Role after the `compat` delta:

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
  `thinking`; the root effort is translated into the OR object (DeepInfra,
  `reasoning_effort_requires_object: true`) or passed through on legacy OR
  deployments. Reasoning OFF → `kill_switch_rescue` to the OR OFF object —
  0 tokens (historical Baidu measurement: was 47–57 and billed).

> If the bound OpenRouter model rejects the raw root `reasoning_effort`
> (LiteLLM's provider map lags the model id), set
> `model_info.metadata.reasoning_effort_requires_object: true` on that
> deployment. The adapter then sends the effort inside OpenRouter's
> `reasoning` object instead. Leave it unset/false for routes that accept the
> root key.

> Effort **level** caveat (historical measurement 2026-09-12 on the former
> Baidu route, LiteLLM v1.99.0): on native, LiteLLM discards the level
> ([litellm #27439]); on OR the provider honoured it erratically. The current
> DeepInfra route shows ordered low/high/max in the 2026-10-20 live checks,
> but do not generalize that to other pinned providers. Treat only the ON/OFF
> kill switch as portable. See `DESIGN.md` §1.4 and `PLAN.md`.

## 4. Troubleshooting — LiteLLM `reasoning_content` warning

LiteLLM emits this warning on the native DeepSeek route when thinking mode is
active and an assistant message has no non-empty `reasoning_content`:

```text
DeepSeek thinking mode: assistant message is missing `reasoning_content`
and none was saved in `provider_specific_fields`. A single-space placeholder
is being injected...
```

It is not a 4xx: LiteLLM injects `" "` to satisfy the API. It does mean the
model receives a blank reasoning chain for that turn, which can degrade
multi-turn and tool-calling quality.

Causes and checks:

1. The extension scope does not list the current model ids (see §2). This is
   the most common cause after a model rename.
2. The client sends `reasoning_content: ""`; LiteLLM treats an empty string
   as missing. The extension upgrades it to `" "` in tool scope.
3. The conversation has no `tools` and no assistant `tool_calls`, so the
   extension's tool-scope check does not fire. Pi core can still send `""`.
4. A persisted/resumed session lost the real reasoning text. The extension
   restores the `reasoning_content` signature where possible.

Fix the client history; do not silence the warning at the gateway. The
`reasoning_route_adapter` never rewrites `messages` (DESIGN §3).

## References

- Gateway behavior, rules and evidence: [`DESIGN.md`](DESIGN.md)
  (§1.2 client contract, §4.3–§4.5 normalization, §6.2 live acceptance).
- Extension: <https://github.com/amartinr/pi-deepseek-reasoning-chain-fix>.
