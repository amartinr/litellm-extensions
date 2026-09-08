# DESIGN — Reasoning-payload normalization at the LiteLLM gateway

Status: ready for implementation
Branch: `main` (repo `litellm-extensions`)
Reference code to mirror: `time_router.py` (module layout, config loading,
logging, registration). Live evidence: `probes/` (2026-09-08) and the
results recorded in `probes/README.md`.

This document is self-contained: an agent without session context must be
able to implement `reasoning_route_adapter.py` from it alone. Where a
decision depends on evidence, the evidence and its source are cited.

---

## 1. Context

### 1.1 Deployment

LiteLLM proxy v1.99.0, DB-less, single `config.yaml`. The gateway serves
DeepSeek models through two upstreams with different reasoning dialects:

| Upstream | model_list entry | reasoning control (chat completions) | assistant-message reasoning |
|---|---|---|---|
| Native DeepSeek (`api.deepseek.com/v1`) | `deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro` | `thinking: {type: enabled\|disabled}` + root `reasoning_effort: low\|high\|max` | `reasoning_content` (required on every assistant message of a tool-calling history; missing → 400 on the raw API) |
| OpenRouter (Baidu fp8) | `openrouter/deepseek-v4-flash` (upstream `deepseek/deepseek-v4-flash-0731`, `provider.order: ["baidu/fp8"]`) | OR object `reasoning: {enabled, effort}` (per-model metadata: `supported_efforts ["max","high","low"]`, `default_effort "high"`, `mandatory false`) | `reasoning` (canonical) or `reasoning_content` (documented alias, "functions identically") |

`time_router.py` (pre-call hook, registered first) reroutes the public alias
`litellm/deepseek-v4-flash` to one of the two upstreams by clock and session
stickiness. `router_settings.fallbacks` can also redirect
`deepseek/deepseek-v4-flash` → `openrouter/deepseek-v4-flash` after a
failure.

### 1.2 Client contract (assumption — read first)

All current LiteLLM clients speak the **DeepSeek-native dialect** (or send no
reasoning control at all). They never send the OR `reasoning` object:

- **pi coding agent** → `deepseek/deepseek-v4-flash` (native), model config
  static in `~/.pi/agent/models.json`. Sends assistant messages with
  `reasoning_content` (forced/replayed by the
  `pi-deepseek-reasoning-chain-fix` extension when the history has tool
  calls). Reasoning control is native: `thinking:{type:"disabled"}` when the
  user disables reasoning; root `reasoning_effort` when the user sets a
  level. Reaches OR only via router fallback.
- **Open WebUI pipe (`agent_loop_guard`)** → `litellm/deepseek-v4-flash`
  (alias, rerouted by `time_router`). Sends assistant messages with
  `reasoning_content`; sends **no** reasoning control params (OWUI filters
  do not run on pipe models).
- The OR object `reasoning:{enabled,effort}` is only emitted by the probe
  scripts in `probes/` and by any future client that adopts it. It is not
  emitted by today's clients.

Consequence: the **active normalization path is OR-bound native-dialect
control** (a native-dialect payload landing on OpenRouter). The object→native
translation is a dormant safeguard for a client that adopts the object.

### 1.3 Measured behavior driving the design (live, effort low; `probes/`, 2026-09-08)

| # | Payload control | Native DeepSeek | OR → Baidu |
|---|---|---|---|
| 1 | `thinking:{type:"disabled"}` | honored (native kill-switch; 0 deltas verified in companion repo) | **ignored** — still reasoned, 47–57 reasoning tokens, 3 runs |
| 2 | `reasoning:{enabled:false, effort:"none"}` | **ignored** — still reasoned, 152/49 tokens, 2 runs | honored — 0 tokens, 3+ runs |
| 3 | `reasoning:{enabled:true, effort:"low"}` | indeterminate vs default (n=2) | honored but over-spends: 164–256 tokens on a trap prompt vs 47–109 default |
| 4 | root `reasoning_effort:"low"` | honored (native vocab) | cheap: 49–64 tokens |
| 5 | assistant messages with `reasoning_content` | required (native field) | accepted alias (OR docs + probe 2) |
| 6 | missing `reasoning_content` on tool history | 400 (raw) / placeholder+warning (LiteLLM) | tolerated (no 4xx) |

Derived rules (do not deviate without new evidence):

- The **only** control a current client sends that a route mishandles is
  `thinking:{type:"disabled"}` landing on OR (row 1): the user asked for no
  reasoning and pays for it silently. This is the problem this hook fixes.
- The OR object must **never** be synthesized for ON on the OR route
  (rows 3–4): `reasoning:{effort:...}` over-spends there while root
  `reasoning_effort` is cheap.
- Assistant messages are **never** rewritten: `reasoning_content` is native
  on DeepSeek and an accepted alias on OR (row 5). No
  `reasoning_content` → `reasoning` rename (Option A decision).
- On the native route, native control already works (row 1 native column);
  only an incoming OR object needs translation (row 2 native column).

## 2. Goals

1. Each upstream receives reasoning control it honors, for the payload the
   client actually sent. Concretely: `thinking:{type:"disabled"}` arriving on
   the OR route disables reasoning.
2. Zero client changes.
3. Minimal, evidence-based transformations only.
4. Deterministic, idempotent, fail-open, stateless, no I/O.

## 3. Non-goals

- Renaming `reasoning_content` → `reasoning` on OR-bound messages (§1.3, row 5).
- Translating root `reasoning_effort` into the OR object on OR-bound requests
  (§1.3, rows 3–4).
- Changing what clients send; per-model callback config (unsupported).
- Solving the object-ON over-spend on OR (client-contract decision, tracked
  separately).

## 4. Module specification — `reasoning_route_adapter.py`

### 4.1 Layout and registration

File at repo root (mounted at `/app/` next to `config.yaml` and
`time_router.py`). Mirror `time_router.py`:

- `CONFIG_PATHS` and a module-level `ROUTE_MAP: dict[str, str]` built from
  `model_info.metadata.route` per `model_list` entry (copy
  `time_router._load_route_map`; keep the module import safe — no
  `litellm.proxy` imports at module level).
- `class ReasoningRouteAdapter(CustomLogger)` implementing
  `async async_pre_call_hook(self, user_api_key_dict, cache, data, call_type)`
  → returns `data`.
- Module instance `proxy_handler_instance = ReasoningRouteAdapter()`.

Registration (order matters — `time_router` first, it reroutes; the adapter
reads the rerouted model):

```yaml
litellm_settings:
  callbacks:
    - 'prometheus'
    - 'time_router.proxy_handler_instance'
    - 'reasoning_route_adapter.proxy_handler_instance'
```

### 4.2 Input schema (`data` in `async_pre_call_hook`)

`data` is the request dict, mirroring the client body at its root keys. The
adapter reads/mutates only:

| Key | Type | Notes |
|---|---|---|
| `data["model"]` | str | Effective model after `time_router` reroute. Read-only. |
| `data["thinking"]` | dict | Native control `{type: "enabled"\|"disabled"}` (clients) |
| `data["reasoning"]` | dict | OR object `{enabled: bool, effort: str}` (probes/future clients) |
| `data["reasoning_effort"]` | str | Root native/OpenAI-style effort (clients) |
| `data["messages"]` | list | **Never modified.** |
| everything else | — | Never touched. |

Presence semantics: absent key → not sent. `data["thinking"]` and
`data["reasoning"]` may coexist (dual-spelling clients); the adapter makes
them consistent per route (see 4.5).

### 4.3 Route classification

`route = ROUTE_MAP.get(data["model"])`:

- `route == "deepseek"` → native-bound.
- `route == "baidu/fp8"` → OR-bound.
- model not in `ROUTE_MAP` (unlisted models, e.g. `anthropic/...`, or the
  alias if `time_router` did not reroute) → **no-op** (return `data`
  unchanged; DEBUG log).

Do not hardcode model names; derive everything from `ROUTE_MAP` (config
driven, same source as `time_router`). If new route labels appear in config,
extend the two label sets via env/constant (defaults: native `{"deepseek"}`,
OR `{"baidu/fp8"}`) — see 4.6.

### 4.4 OR-bound rules (route label `baidu/fp8`)

Goal: make native-dialect control behave on OR. Rules, evaluated on the
request dict, in order:

1. `thinking` present:
   - `thinking.type == "disabled"` → **rescue**: set
     `reasoning = {"enabled": false, "effort": "none"}`; delete `thinking`;
     delete `reasoning_effort` if present. (OR ignores `thinking`, row 1;
     object OFF is its documented contract, row 2.)
   - `thinking.type == "enabled"` → delete `thinking` only (OR ignores it;
     thinking is the provider default anyway; never synthesize an object —
     rows 3–4).
2. `reasoning` present → leave as-is (already OR dialect).
3. Root `reasoning_effort` present (no `thinking`) → leave as-is (cheap on
   OR, row 4).
4. Nothing reasoning-related present → no-op.

Example (pi fallback to OR, reasoning off):

```json
// in:  { "model": "openrouter/deepseek-v4-flash",
//        "thinking": { "type": "disabled" },
//        "reasoning_effort": "low" }
// out: { "model": "openrouter/deepseek-v4-flash",
//        "reasoning": { "enabled": false, "effort": "none" } }
```

### 4.5 Native-bound rules (route label `deepseek`)

Goal: pass through native-dialect payloads untouched (they already work) and
translate an incoming OR object so OFF/effort behave on native (row 2).

1. `reasoning` present:
   - OFF: `reasoning.enabled is False` or `reasoning.effort == "none"` →
     set `thinking = {"type": "disabled"}`; delete `reasoning`; delete
     `reasoning_effort` if present.
   - ON: `reasoning.enabled is True` and `reasoning.effort` maps per the
     vocabulary table → set `thinking = {"type": "enabled"}` and
     `reasoning_effort = <mapped>`; delete `reasoning`.
   - Effort vocabulary (DeepSeek collapse table; unknown/unmappable value →
     omit `reasoning_effort`, thinking enabled only):

     | input effort | mapped `reasoning_effort` |
     |---|---|
     | `low`, `high`, `max` | same |
     | `medium`, `xhigh` | `high` |
     | `minimal` | `low` |
     | `none` | handled by the OFF branch |
2. `thinking` present (native dialect) → leave as-is (native honors it).
3. Root `reasoning_effort` present → leave as-is.
4. Nothing reasoning-related present → no-op.

Example (probe/future client with OR object, OFF, native route):

```json
// in:  { "model": "deepseek/deepseek-v4-flash",
//        "reasoning": { "enabled": false, "effort": "none" } }
// out: { "model": "deepseek/deepseek-v4-flash",
//        "thinking": { "type": "disabled" } }
```

### 4.6 Execution rules

- `REASONING_ADAPTER_DISABLED=1` (env) → return `data` unchanged (rollback).
- Fail-open: wrap in `try/except`; on exception log once (rate-limited, copy
  `time_router._rate_limited_warning`) and return `data` unchanged.
- Idempotent: re-running over an already-normalized payload is a no-op
  (check rules: `thinking` gone, `reasoning` set → no further change).
- `data` keys are deleted with `data.pop(key, None)`; values set in place.
- Logging via lazy `verbose_proxy_logger` import (copy `time_router._log`).
  Always log one line per applied transformation:
  `ReasoningAdapter: model=... route=... action=kill_switch_rescue|drop_thinking|object_to_native|...`.
  `REASONING_ADAPTER_DEBUG=1` → log the full before/after reasoning keys.

## 5. Config reference addition

Only the registration block of §4.1. No other `config.yaml` change.

## 6. Acceptance criteria (definition of done)

### 6.1 Offline (no network)

Pure-function checks in the repo venv (stdlib only):

1. Classification: model→route via a fixture `ROUTE_MAP`
   (`deepseek/...` → `deepseek`, `openrouter/...` → `baidu/fp8`, unknown →
   no-op).
2. §4.4 rule 1 (disabled → object OFF + cleanup) and rule 1b (enabled →
   drop thinking); before/after byte-exact vs the examples.
3. §4.5 OFF and ON translations incl. the vocabulary table rows.
4. Idempotency: applying twice → second is a no-op (returns unchanged, no
   log).
5. Fail-open: malformed values (e.g. `thinking: "junk"`) → unchanged data,
   no exception.
6. Native-dialect payloads (thinking/root, no object) pass through
   byte-identical on both routes.
7. `messages` untouched in every case.

### 6.2 Live (gateway, one provider per test; key from env, effort low)

Reuse `probes/` conventions (`probes/reasoning_format_tolerance.py` with
`LITELLM_MODEL=...` for per-route runs). Expected after deployment:

1. OR route (`openrouter/deepseek-v4-flash`) with body
   `thinking:{type:"disabled"}` → **0 reasoning tokens** (was 47–57).
2. Native route (`deepseek/deepseek-v4-flash`) with body
   `reasoning:{enabled:false, effort:"none"}` → **0 reasoning tokens**
   (was 152/49).
3. Native route with `reasoning:{enabled:true, effort:"low"}` → low-range
   reasoning (~50 tokens on the probe prompt).
4. Regression: native-dialect and no-control payloads produce identical
   results with the hook on vs off (compare with
   `REASONING_ADAPTER_DISABLED=1`).
5. Fallback path: force a direct-call failure so the router falls back to
   `openrouter/...`, with the client payload carrying `thinking:disabled` —
   verify whether the pre-call hook re-runs on the fallback attempt and the
   rescue applies (see 7).

## 7. Open questions (resolve during implementation/deployment)

- **Pre-call hook ordering** in v1.99.0: confirm with one DEBUG log in the
  adapter that `data["model"]` is the rerouted name (time_router first in
  `callbacks`). If the alias model appears un-rewritten, `ROUTE_MAP` lookup
  decides (no-op unless the alias carries a route label).
- **Fallback re-execution** (§6.2 item 5): whether `async_pre_call_hook`
  runs again per fallback attempt is not assumed. If it does not, the
  pi-direct → OR fallback with `thinking:disabled` is not covered; decide
  then (options: client dual-spelling OFF, or accept the gap).
- LiteLLM's own DeepSeek transformation (`transformation.py`, placeholder
  injection when `reasoning_content` is missing) is orthogonal: clients
  already carry the field (§1.3 row 5); no interaction expected.

## 8. Evidence references

- `probes/reasoning_format_tolerance.py`, `probes/tool_replay_tolerance.py`,
  `probes/README.md` (results recorded 2026-09-08, commits on `main`).
- OR metadata for `deepseek/deepseek-v4-flash-0731`: `GET
  https://openrouter.ai/api/v1/models` (public).
- DeepSeek thinking-mode docs: `api-docs.deepseek.com/guides/thinking_mode/`.
