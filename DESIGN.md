# DESIGN — Reasoning-payload normalization for the OpenRouter route

Status: draft for review
Repo: litellm-extensions (gateway hook alongside `time_router.py`)
Companion evidence: `probes/` (live tolerance results, 2026-09-08)

## 1. Rationale

### 1.1 Problem

The gateway serves the same DeepSeek model family through two upstreams with
different reasoning dialects:

- **Native DeepSeek** (`api.deepseek.com`, deployments `deepseek/deepseek-v4-flash`,
  `deepseek/deepseek-v4-pro`): chat-completions reasoning control is
  DeepSeek-native — `thinking: {type: enabled|disabled}` + root
  `reasoning_effort: low|high|max`. Assistant-message reasoning is carried in
  `reasoning_content` and **required** on every assistant message of a
  tool-calling history (missing field → 400 on the raw API; LiteLLM injects a
  `" "` placeholder + warning).
- **OpenRouter** (`openrouter.ai`, deployment `openrouter/deepseek-v4-flash` →
  Baidu fp8): reasoning control is the OR object
  `reasoning: {enabled, effort}` (its documented contract; per-model metadata
  for `deepseek-v4-flash-0731`: `supported_efforts ["max","high","low"]`,
  `default_effort "high"`, `mandatory false`). Assistant-message reasoning may
  use OR's `reasoning` field or `reasoning_content`, which OR documents as an
  alias ("functions identically").

The client cannot know which upstream will serve a request: `time_router`
reroutes the `litellm/deepseek-v4-flash` alias by clock (off-peak → native,
peak → OR), and `router_settings.fallbacks` can redirect `deepseek/...` to
`openrouter/...` after a failure. A single client payload therefore reaches
whichever endpoint LiteLLM selects, and each endpoint ignores or mis-handles
the other dialect's control.

### 1.2 Measured consequences (live, OR → Baidu and native, effort low unless noted)

| # | Payload control | Native DeepSeek | OR → Baidu | Source |
|---|---|---|---|---|
| 1 | `thinking:{type:"disabled"}` | honored (native kill-switch; 0 deltas verified in companion-repo probes) | **ignored** — still reasoned, 47–57 reasoning tokens, 3 runs | probes 2026-09-08 |
| 2 | `reasoning:{enabled:false, effort:"none"}` | **ignored** — still reasoned, 152/49 tokens, 2 runs | honored — 0 tokens, 3+ runs | probes 2026-09-08 |
| 3 | `reasoning:{enabled:true, effort:"low"}` (and simplified object) | indeterminate vs default (51/89 vs 23/82, n=2) | honored but over-spends: 164–256 tokens on a trap prompt vs 47–109 default | probes 2026-09-08 |
| 4 | root `reasoning_effort:"low"` | honored (native vocab) | cheap: 49–64 tokens (kept out of the current probe suite per review; git history) | probes 2026-09-08 |
| 5 | assistant messages with `reasoning_content` | required (native field) | accepted alias of `reasoning` | OR docs + probe 2 |
| 6 | missing `reasoning_content` on tool history | 400 (raw) / placeholder+warning (LiteLLM) | tolerated (no 4xx); real-text replay gives the richest continuation | probes 2026-09-08 |

Consequences:

- **Kill-switch is lost on OR**: a client that disables reasoning with the
  DeepSeek-native switch (what the pi agent sends when the user turns
  reasoning off) still pays for reasoning when the request lands on OR
  (peak alias traffic, or a direct-call fallback). No error is raised — the
  failure is silent and costs money.
- **OR-object OFF is lost on native**: a client that speaks the OR object
  (the standardized contract form) cannot disable reasoning on the native
  route (rows 2 vs 1).
- No single client control spelling works on both upstreams for OFF (rows 1
  and 2 are complementary), and the cheapest ON spelling differs per route
  (rows 3 and 4).

### 1.3 Why normalize at the gateway

The route is only known inside LiteLLM, after `time_router`'s reroute
decision. LiteLLM config has no per-model callback attachment (callbacks in
`litellm_settings` are global), so the documented mechanism is a global
`CustomLogger` that filters by model inside its `async_pre_call_hook` —
exactly the pattern `time_router` already uses. A separate hook module keeps
the concerns apart (routing vs payload normalization).

### 1.4 Goals

1. Each upstream receives reasoning control it honors: OFF works on both
   routes with the payload the client actually sent.
2. Zero client changes: Open WebUI pipe and pi agent payloads stay as-is.
3. Minimal, evidence-based transformations only — no speculative rewrites
   that could regress cost or behavior.
4. Deterministic, idempotent, fail-open, no state.

### 1.5 Non-goals

- Renaming `reasoning_content` → `reasoning` on OR-bound messages. OR
  documents the alias as identical (row 5); no functional difference was
  measured (probe 2 legs all 200 with `reasoning_content` replay), and the
  native route requires exactly `reasoning_content`. Message replay stays
  `reasoning_content` on both routes (round-trip consistent with what
  LiteLLM returns to clients).
- Translating root `reasoning_effort` into the OR object on OR-bound
  requests. Evidence (rows 3–4): the object over-spends on OR while the root
  spelling is cheap; translating would regress cost.
- Per-model callback attachment in config.yaml (not supported by LiteLLM).
- Changing what clients send.

## 2. Design

### 2.1 Module and registration

New file `reasoning_route_adapter.py` in this repo, mounted at `/app/`
alongside `time_router.py` (the proxy working directory). Module-level
instance, registered after `time_router` so it sees the rerouted model:

```yaml
litellm_settings:
  callbacks:
    - 'prometheus'
    - 'time_router.proxy_handler_instance'                  # 1. route decision
    - 'reasoning_route_adapter.proxy_handler_instance'       # 2. payload normalization
```

Class: `ReasoningRouteAdapter(CustomLogger)` implementing
`async_pre_call_hook(self, user_api_key_dict, cache, data, call_type)`,
returning `data` (mutated) or `data` unchanged. Same lazy-logger pattern as
`time_router` (`verbose_proxy_logger`; JSON lines when `json_logs: true`).

### 2.2 Route classification

Classify the request from `data["model"]` after `time_router` has run:

- Build the model → route map from `config.yaml` the same way `time_router`
  does (read `model_info.metadata.route` per `model_list` entry from
  `LITELLM_CONFIG_FILE` / `/app/config.yaml`). Route label `"deepseek"` →
  native upstream; `"baidu/fp8"` → OR upstream.
- Unknown models (e.g. `anthropic/...`) → no-op.
- If `time_router` is disabled, direct calls still carry their model name and
  classify correctly; the alias `litellm/deepseek-v4-flash` without reroute
  is not classified (its default deployment is OR) — document as a fallback
  case.

### 2.3 OR-bound transformations (route label `baidu/fp8`)

Single purpose: rescue the DeepSeek-native control that OR ignores.

| Condition (request body) | Transformation | Rationale |
|---|---|---|
| `thinking` present and `thinking.type == "disabled"` | Set `reasoning = {"enabled": false, "effort": "none"}` (explicit keys per payload contract); remove `thinking` | OR ignores `thinking` (row 1); the object OFF is its documented contract (row 2) |
| `thinking` present and `thinking.type == "enabled"` | Remove `thinking` only | OR ignores it; thinking is the provider default anyway; do not synthesize an object (row 3: object ON over-spends) |
| root `reasoning_effort` present | Leave untouched | Cheap on OR (row 4); do not translate into the object |
| `reasoning_content` on assistant messages | Leave untouched | Accepted alias (row 5) |

Before/after (kill-switch rescue):

```json
// client (pi agent, reasoning off, direct-call fallback to OR):
{ "model": "openrouter/deepseek-v4-flash",
  "messages": [ { "role": "assistant", "content": "...", "reasoning_content": "..." } ],
  "thinking": { "type": "disabled" } }

// upstream payload after adapter:
{ "model": "openrouter/deepseek-v4-flash",
  "messages": [ /* unchanged */ ],
  "reasoning": { "enabled": false, "effort": "none" } }
```

### 2.4 Native-bound transformations (route label `deepseek`)

Pass through payloads that already speak native (pi: `thinking` + root
`reasoning_effort`; OWU: no control). Only translate when a client sends the
OR object — the standardized contract form — which native ignores (row 2):

| Condition | Transformation |
|---|---|
| `reasoning.enabled == false` or `reasoning.effort == "none"` | Set `thinking = {"type": "disabled"}`; remove `reasoning`; remove root `reasoning_effort` if present |
| `reasoning.enabled == true` and `reasoning.effort` in `low/high/max` | Set `thinking = {"type": "enabled"}` and `reasoning_effort = <effort>`; remove `reasoning`. Map OR-only values per DeepSeek's table: `medium → high`, `xhigh → high` |
| `reasoning` absent | Leave untouched (payload already native or empty control) |
| assistant messages | Leave untouched (`reasoning_content` is the native field) |

Before/after (object-speaking client, OFF, native route):

```json
// client:
{ "model": "deepseek/deepseek-v4-flash",
  "messages": [ { "role": "assistant", "content": "...", "reasoning_content": "..." } ],
  "reasoning": { "enabled": false, "effort": "none" } }

// upstream payload after adapter:
{ "model": "deepseek/deepseek-v4-flash",
  "messages": [ /* unchanged */ ],
  "thinking": { "type": "disabled" } }
```

### 2.5 Execution rules

- Fail-open: every transformation wrapped; on any exception, log once
  (rate-limited) and return `data` unchanged.
- Deterministic and idempotent: pure dict operations on `data`; re-running
  over an already-normalized payload changes nothing.
- Never touches `messages` content, `tools`, `stream`, or non-reasoning
  params.
- Only the outbound hop is affected; responses and client-visible behavior
  are unchanged.

### 2.6 Configuration and observability

Env knobs (optional):

| Variable | Default | Purpose |
|---|---|---|
| `REASONING_ADAPTER_DISABLED` | unset | Hard disable (testing/rollback) |
| `REASONING_ADAPTER_DEBUG` | unset | Verbose per-request logging (model, route, transformations applied) |

Logging: always emit one line when a transformation is applied
(`ReasoningAdapter: route=... model=... action=...`); detail behind DEBUG.
Rate-limit failure warnings (same helper as `time_router`).

## 3. Config reference addition

`config.yaml.example`: register the hook after `time_router` (section 2.1).
No other config changes required.

## 4. Verification plan

### 4.1 Offline

Pure-function checks (no network): classification (model → route), each
transformation's before/after, idempotency, fail-open on malformed payloads,
non-reasoning models untouched. Runnable with the repo venv.

### 4.2 Live (gateway, one provider per test)

Reuse `probes/` conventions (env key only, effort low, small payloads):

1. OR alias with `thinking:{type:"disabled"}` → expect 0 reasoning tokens
   (was 47–57 without the hook).
2. Native with the OR object OFF → expect 0 reasoning tokens (was 152/49).
3. Native with the OR object ON at `low` → expect low-range reasoning.
4. Regression: no-control and native-dialect requests produce identical
   payloads with and without the hook (log comparison, DEBUG on).
5. Fallback path: force a direct-call failure → OR fallback request — verify
   whether pre-call hooks run again on the fallback attempt and the
   kill-switch rescue applies (see 5).

## 5. Risks and open questions

- **Pre-call hook ordering**: the design depends on `time_router` running
  before the adapter within `litellm_settings.callbacks`. Verify execution
  order in v1.99.0 with one DEBUG log line (adapter logs the model it sees).
- **Fallback attempts**: whether `async_pre_call_hook` re-runs for the
  fallback deployment (with the fallback model name) is not assumed — item
  4.5 verifies it; if hooks do not re-run, the pi-direct → OR fallback case
  is not covered by the adapter and needs a decision (e.g. client-side
  dual-spelling OFF).
- **Object ON over-spend on OR** (row 3) is not solved by this hook — it is
  intrinsic to the object spelling on OR. If cost control for ON matters,
  the spelling decision (root vs object) belongs to the client contract and
  is tracked separately.
- **LiteLLM's own DeepSeek transformation** (`transformation.py`, placeholder
  injection when `reasoning_content` is missing) is orthogonal: the clients
  already carry the field (row 5); no interaction expected.
