# DESIGN — Reasoning-payload normalization at the LiteLLM gateway

Status: implemented at request level (offline acceptance green, 16/16 in
`tests/test_reasoning_route_adapter.py`, stdlib-only venv). Target
architecture — per-deployment hook (§4.1, §7) and shared config loader
(§5.2) — pending implementation. Live checks of §6.2 pending gateway
deployment.
Branch: `main` (repo `litellm-extensions`).
Reference code: `../time_router/time_router.py` (module layout, registration;
config loading/logging via the shared `hook_config`, §5.2). Live evidence:
`probes/` and `probes/README.md`.

Self-contained specification for `reasoning_route_adapter.py`. Decisions
that depend on evidence cite the source.

---

## 1. Context

### 1.1 Deployment

LiteLLM proxy v1.99.0, DB-less, single `config.yaml`. The gateway serves
DeepSeek models through two upstreams with different reasoning dialects:

| Upstream | model_list entry | reasoning control (chat completions) | assistant-message reasoning |
|---|---|---|---|
| Native DeepSeek (`api.deepseek.com/v1`) | `deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro` | `thinking: {type: enabled\|disabled}` + root `reasoning_effort: low\|high\|max` | `reasoning_content` (required on every assistant message of a tool-calling history; missing → 400 on the raw API) |
| OpenRouter (Baidu fp8) | `openrouter/deepseek-v4-flash` (upstream `deepseek/deepseek-v4-flash-0731`, `provider.order: ["baidu/fp8"]`) | OR object `reasoning: {enabled, effort}` (per-model metadata: `supported_efforts ["max","high","low"]`, `default_effort "high"`, `mandatory false`) | `reasoning` (canonical) or `reasoning_content` (documented alias, "functions identically") |

The `time_router` hook (request-level pre-call, registered first) reroutes the
public alias `litellm/deepseek-v4-flash` to one of the two upstreams by clock
and session stickiness. It must stay at request level: it chooses the
deployment by rewriting `data["model"]` before routing.
`router_settings.fallbacks` can also redirect `deepseek/deepseek-v4-flash` →
`openrouter/deepseek-v4-flash` after a failure. The adapter runs on the
per-deployment pre-call hook (`async_pre_call_deployment_hook`, §4.1), so it
sees the deployment bound for each attempt — including retries and fallback
steps.

### 1.2 Client contract (assumption)

Both current LiteLLM clients (Open WebUI and pi) run an extension that
formats their DeepSeek requests to the **DeepSeek-native dialect** — the raw
HTTP contract of `api.deepseek.com`: root keys `thinking:{type:enabled|disabled}`
and `reasoning_effort`, assistant messages carrying `reasoning_content` (see
its curl example; the OpenAI-SDK `extra_body` wrapper is an SDK artifact, not
the wire format LiteLLM forwards). So requests arriving at the gateway for
DeepSeek models are assumed to already speak the native dialect (or send no
reasoning control at all); they never send the OR `reasoning` object:

- **pi coding agent** → two LiteLLM configs, both sending the native
  dialect once the §9 models.json compat delta is applied (pi core emits it;
  the extension `pi-deepseek-reasoning-chain-fix` refines it where scoped):
  assistant `reasoning_content` forced/replayed in tool scope;
  reasoning control native — `thinking:{type:"disabled"}` when the user
  disables reasoning, root `reasoning_effort` when the user sets a level
  (both keys may coexist, per the DeepSeek curl example). Model config
  static in `~/.pi/agent/models.json`:
  - alias `litellm/deepseek-v4-flash` → the main path: time_router reroutes
    it, so in peak windows native-dialect payloads land on OR — the
    adapter's active normalization path.
  - LiteLLM configured in pi as a `deepseek` provider requesting the
    gateway entry `deepseek/deepseek-v4-flash` → served native always
    (time_router labels only; no rerouting, hence no peak avoidance on this
    path). Native dialect against the native API: no normalization needed.
    It reaches OR only when `router_settings.fallbacks` redirects after a
    native failure — the case the adapter's per-deployment hook now covers
    (§4.1, §7).
  A provider pointing straight at `api.deepseek.com` (no gateway at all) is
  the only config the hooks never see; it is not used in this deployment.

  Request construction is pi-side and **provider-driven** (evidence: pi
  source, `openai-completions` transport, `detectCompat`): the dialect pi
  emits follows the models.json provider key name / baseUrl, not the model
  id. A provider keyed `deepseek` (or a baseUrl containing `deepseek.com`)
  selects the native dialect unconditionally: `thinkingFormat:"deepseek"`
  sends `thinking:{type:enabled}` + root `reasoning_effort` when reasoning
  is on, `thinking:{type:disabled}` when off (level values via the model's
  `thinkingLevelMap`, defaulting to the verbatim pi level);
  `requiresReasoningContentOnAssistantMessages:true` replays
  `reasoning_content` on every assistant message (real text when the turn
  reasoned, `""` otherwise), satisfying the native 400 rule for tool
  histories. So pi emits native dialect regardless of where the gateway
  routes the request, and the adapter reconciles by effective route.
  Corollary: a pi provider keyed `openrouter` pointed at the gateway would
  emit the OR object `reasoning:{effort}` — the dialect that makes the
  native-bound object→native translation (§4.5) live, not just a probe
  artifact.

  This deployment's `models.json` is the provider keyed `litellm` (baseUrl =
  gateway, `api: openai-completions`): detectCompat classifies it as generic
  OpenAI (`thinkingFormat: "openai"`), so today reasoning ON emits root
  `reasoning_effort` but reasoning OFF emits **nothing** — no kill switch
  reaches the gateway (silent over-spend on both routes). The §9 compat
  delta makes pi emit the native dialect assumed above; the adapter's
  peak-hour fix is coupled to it (deploy together). The extension is scoped
  to `deepseek/...` ids and does not cover the alias model.
- **Open WebUI pipe (`agent_loop_guard`)** → `litellm/deepseek-v4-flash`
  (alias, rerouted by `time_router`). Sends assistant messages with
  `reasoning_content`; sends **no** reasoning control params (OWUI filters
  do not run on pipe models; its extension keeps the history
  native-conformant).
- The OR object `reasoning:{enabled,effort}` is only emitted by the probe
  scripts in `probes/` and by any future client that adopts it. It is not
  emitted by today's clients.

Consequence: the **active normalization path is OR-bound native-dialect
control** (a native-dialect payload landing on OpenRouter). The object→native
translation is a dormant safeguard for a client that adopts the object — or
for a pi provider keyed `openrouter` pointed at the gateway (see above).

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

`reasoning_route_adapter/reasoning_route_adapter.py`, mounted at
`/app/reasoning_route_adapter.py` next to `config.yaml` and the
`time_router` module. Module layout:

- `ROUTE_MAP` (`model_name → route`) and `DIALECT_MAP`
  (`model_name → reasoning_dialect`), derived from the shared config loader
  `hook_config` (§5.2): it reads `config.yaml` once and exposes per-model
  descriptors (`route`, `reasoning_dialect`, `time_router`). Mounted at
  `/app/hook_config.py` alongside the two hooks; keep the module import safe
  — no `litellm.proxy` imports at module level.
- `class ReasoningRouteAdapter(CustomLogger)` implementing
  `async async_pre_call_deployment_hook(self, kwargs, call_type)`
  → returns `kwargs` (or `None` to leave the chain unchanged). It runs once
  per real deployment attempt (original, retry, fallback step).
- Module instance `proxy_handler_instance = ReasoningRouteAdapter()`.

Registration (order no longer affects the adapter: it classifies by the bound
deployment, not by `data["model"]`, so it does not depend on `time_router`
having run first; the order is kept as-is):

```yaml
litellm_settings:
  callbacks:
    - 'prometheus'
    - 'time_router.proxy_handler_instance'
    - 'reasoning_route_adapter.proxy_handler_instance'
```

### 4.2 Input schema (`kwargs` in `async_pre_call_deployment_hook`)

`kwargs` is the completion request kwargs at the SDK boundary. The adapter
reads/mutates only the root reasoning keys; classification reads the bound
deployment from the router metadata bucket (`metadata` or `litellm_metadata`
— check both):

| Key | Type | Notes |
|---|---|---|
| `kwargs["model"]` | str | Provider model sent upstream. **Not** used for classification. |
| `kwargs[metadata]["deployment_model_name"]` | str | `model_list` name of the bound deployment. Classification key. |
| `kwargs[metadata]["model_info"]["metadata"]` | dict | Bound deployment's `model_info.metadata` (`route`, `reasoning_dialect`); preferred over the map lookup. |
| `kwargs["thinking"]` | dict | Native control `{type: "enabled"\|"disabled"}` (clients) |
| `kwargs["reasoning"]` | dict | OR object `{enabled: bool, effort: str}` (probes/future clients) |
| `kwargs["reasoning_effort"]` | str | Root native/OpenAI-style effort (clients) |
| `kwargs["messages"]` | list | **Never modified.** |
| everything else | — | Never touched. |

Presence semantics: absent key → not sent. `kwargs["thinking"]` and
`kwargs["reasoning"]` may coexist (dual-spelling clients); the adapter makes
them consistent per bound route (see 4.4, 4.5).

### 4.3 Route classification (bound deployment)

Config-driven, two layers, evaluated on the **bound deployment** (never on
`kwargs["model"]`, which is the provider model):

- **Primary — declared dialect.** The deployment's
  `model_info.metadata.reasoning_dialect`: `"deepseek"` → native-bound,
  `"openrouter"` → OR-bound. Read from
  `kwargs[metadata]["model_info"]["metadata"]["reasoning_dialect"]`. A
  declared dialect outside this vocabulary → **no-op** (fail-open, DEBUG
  log).
- **Fallback — route-label taxonomy.** When `model_info` is absent (e.g.
  non-router SDK calls), classify by
  `deployment_model_name = kwargs[metadata]["deployment_model_name"]` against
  `DIALECT_MAP` / `ROUTE_MAP` and the label sets (defaults native
  `{"deepseek"}`, OR `{"baidu/fp8"}`).
- No deployment metadata (unlisted models, direct SDK calls) → **no-op**
  (return `kwargs` unchanged; DEBUG log).

Do not hardcode model names; everything comes from `config.yaml`
(`reasoning_dialect` preferred, `route` as fallback — same source as
`time_router`).

### 4.4 OR-bound rules (route label `baidu/fp8`)

Goal: make native-dialect control behave on OR. Rules, evaluated on the
request payload, in order:

1. `thinking` present:
   - `thinking.type == "disabled"` → **rescue**: set
     `reasoning = {"enabled": false, "effort": "none"}`; delete `thinking`;
     delete `reasoning_effort` if present. (OR ignores `thinking`, row 1;
     object OFF is its documented contract, row 2.)
   - any other `thinking.type`, including `"enabled"` → delete `thinking`
     only (OR ignores it; reasoning defaults ON; never synthesize an object —
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

- Runs once per real deployment attempt (original, retry, fallback step):
  the SDK wrapper invokes it after the router selects the deployment and
  before the request is sent.
- `REASONING_ADAPTER_DISABLED=1` (env) → return `kwargs` unchanged (rollback).
- Fail-open: wrap in `try/except`; on exception emit a warning at most once
  per 5 min (`_rate_limited_warning`) and return `kwargs` unchanged.
- Idempotent: re-running over an already-normalized payload is a no-op
  (check rules: `thinking` gone, `reasoning` set → no further change). This
  matters because the same `kwargs` can be reused across attempts.
- `kwargs` keys are deleted with `kwargs.pop(key, None)`; values set in
  place. Return `kwargs` (or `None` to leave the chain unchanged).
- Metadata bucket: read `kwargs["metadata"]` and `kwargs["litellm_metadata"]`
  (the router picks one via `_get_router_metadata_variable_name`); use
  whichever is present.
- Logging via lazy `verbose_proxy_logger` import (copy `time_router._log`).
  Always log one line per applied transformation:
  `ReasoningAdapter: model=... route=... action=kill_switch_rescue|drop_thinking|object_to_native|...`.
  `REASONING_ADAPTER_DEBUG=1` → log the full before/after reasoning keys.

## 5. Config surfaces and shared loader

### 5.1 Surfaces

- **Per-model facts** → `model_info.metadata` (already used):
  `reasoning_dialect` (§4.3 primary classification) and `route` (label +
  fallback taxonomy) — see `../config.yaml.example`.
- **Hook operation knobs** → `callback_settings.reasoning_route_adapter` at
  the **top level** of `config.yaml`:

  ```yaml
  callback_settings:
    reasoning_route_adapter:
      warning_interval_s: 300
      native_route_labels: ["deepseek"]
      or_route_labels: ["baidu/fp8"]
  ```

  v1.99.0 reads `config.get("callback_settings")` in `proxy_server.py` and
  exposes it as `litellm.callback_settings`; it is **not**
  `litellm_settings.callback_settings`. A `CustomLogger` registered by
  dotted path does not receive its block automatically (only built-in
  callbacks get `callback_specific_params`), so the hook reads it itself via
  `hook_config.settings("reasoning_route_adapter")`. Defaults apply when the
  block or a value is absent or of the wrong type.
- **Provider protocol facts stay in code**: `EFFORT_MAP` and the synthetic
  OFF object (not operator-tunable; must not diverge from provider semantics
  — `probes/`).

Registration is unchanged (§4.1).

### 5.2 Shared loader `hook_config.py`

Decision (pending implementation): the duplicated `CONFIG_PATHS` / YAML read
/ `_log` plumbing, the `callback_settings` reader and the config validation
live in one module, `hook_config.py`, mounted at `/app/hook_config.py` and
imported as top-level `hook_config` by both hooks. API:

- `MODELS` — `{model_name: {route, reasoning_dialect, time_router}}`.
- `settings(hook_name)` — `callback_settings.<hook_name>` with defaults and
  type checks.
- `get_logger()` — the lazy `verbose_proxy_logger` accessor.

Rationale: config I/O, validation and the settings reader exist once, so the
hooks keep only their policy and cannot diverge. Rejected alternative: have
`reasoning_route_adapter` import from `time_router` (avoids a third mount but
couples the two hooks). Cost: a third volume mount (update the root
`README.md` `volumes:` block); `/app` is on `sys.path` at runtime (LiteLLM's
CLI appends `os.getcwd()`), so the import resolves. Not a net line reduction
once validation/settings are included — the win is single-source behavior,
not LOC. Offline tests stub `hook_config` instead of `yaml`/`litellm`.

Error policy (never abort proxy startup):

- unreadable / invalid YAML → `MODELS` empty, hooks no-op, one ERROR log;
- parseable but semantically invalid `time_router` block (target missing,
  `offpeak_target` without `peak_windows`, malformed window) → reroute
  disabled for that alias, ERROR per problem; never an invalid model and
  never `route="unknown"`;
- invalid `callback_settings` value → documented default + ERROR.

## 6. Acceptance criteria (definition of done)

### 6.1 Offline (no network)

Pure-function checks in the repo venv (stdlib only):

1. Classification by the bound deployment: a declared
   `model_info.metadata.reasoning_dialect` wins (`deepseek/...` →
   native-bound, `openrouter/...` → OR-bound), `deployment_model_name` →
   `DIALECT_MAP`/`ROUTE_MAP` fallback for entries without the declaration,
   unknown deployment or unrecognized declared dialect → no-op.
2. §4.4 rule 1: disabled → object OFF + cleanup; other types → drop
   thinking. Before/after byte-exact vs the examples.
3. §4.5 OFF and ON translations incl. the vocabulary table rows.
4. Idempotency: applying twice → second is a no-op (returns unchanged, no
   log).
5. Fail-open: malformed values (e.g. `thinking: "junk"`) → unchanged data,
   no exception.
6. Native-dialect payloads without an object (root `reasoning_effort`, and
   `thinking` on the native route) pass through byte-identical. On the OR
   route `thinking` is normalized per §4.4 rule 1 and is excluded.
7. `messages` untouched in every case.

The module under test reads config via the shared loader (§5.2), so the
offline harness stubs `hook_config` (fixture `MODELS` and `settings()`)
instead of `yaml`/`litellm`, and overrides `ROUTE_MAP`/`DIALECT_MAP` after
import. Payloads are `kwargs`-shaped: a `model`, a metadata bucket carrying
`deployment_model_name` and `model_info.metadata.reasoning_dialect`, plus the
root reasoning keys.

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
   the per-deployment hook must run on the fallback attempt and rescue the
   kill switch to `reasoning:{enabled:false, effort:"none"}`.

## 7. Resolved decisions and remaining open questions

- **Fallback re-execution — resolved: migrate to the per-deployment hook.**
  The request-level `async_pre_call_hook` runs once, before routing, and does
  not re-run on a `router_settings.fallbacks` step. `async_pre_call_deployment_hook`
  is invoked in LiteLLM's SDK wrapper (`litellm/utils.py`) and the router
  dispatches every attempt (original, retry, fallback step) through it, so
  the adapter moves there (§4.1). This closes the gap where a native-dialect
  payload (`thinking:disabled`) redirected to `openrouter/...` was not
  rescued — a whole outage/cooldown window, not a single request.
- **Pre-call hook ordering — resolved.** The adapter classifies by the bound
  deployment (§4.3), not by `data["model"]`, so it no longer depends on
  `time_router` having run first. `time_router` stays request-level (it must
  choose the deployment before routing).
- LiteLLM's own DeepSeek transformation (`transformation.py`, placeholder
  injection when `reasoning_content` is missing) is orthogonal: clients
  already carry the field (§1.3 row 5); no interaction expected.

The timing of the fallback the per-deployment hook now covers is set by
LiteLLM core retry/cooldown knobs (`litellm_params.num_retries` / `timeout`,
`router_settings.allowed_fails` / `cooldown_time`), not by hook config — see
the root `README.md` "Retries and fallback" section for defaults and effects.
Non-retryable errors (400/401/403/404) fall back immediately; retryable ones
(408/409/429/5xx) after `num_retries`.

## 8. Evidence references

- `probes/reasoning_format_tolerance.py`, `probes/tool_replay_tolerance.py`,
  `probes/README.md` (results recorded 2026-09-08, commits on `main`).
- OR metadata for `deepseek/deepseek-v4-flash-0731`: `GET
  https://openrouter.ai/api/v1/models` (public).
- DeepSeek thinking-mode docs (toggle/effort contract, collapse table,
  `reasoning_content` 400 rule): `api-docs.deepseek.com/guides/thinking_mode/`.
  Raw-request curl contract (root `thinking` + `reasoning_effort`):
  `api-docs.deepseek.com`. Both verified at implementation time.
- Response-field drift probe (2026-09-09): `probes/reasoning_response_field.py`
  and results in `probes/README.md` — the route delivers reasoning under
  `reasoning_content` (stream and non-stream, OR and native); ad-hoc
  native-shape tolerance check (n=1) recorded alongside.

## 9. Activation notes (this deployment, 2026-09-09)

The peak-hour fix couples a client-side change with the adapter; they land
**together** (models.json alone fixes off-peak OFF only — native honors the
kill switch; the adapter alone has nothing to rescue — pi must emit it).

### 9.1 pi models.json delta

Current state: provider keyed `litellm` (baseUrl = gateway, `api:
openai-completions`, compat only `supportsDeveloperRole:false` +
`supportsReasoningEffort:true`). detectCompat classifies it as generic
OpenAI (`thinkingFormat: "openai"`): reasoning ON emits root
`reasoning_effort` (honored and cheap on both routes); reasoning OFF emits
nothing — DeepSeek reasons by default and the user pays for reasoning they
disabled (silent over-spend, both routes).

Required compat delta:

    "compat": {
      "supportsDeveloperRole": false,
      "supportsReasoningEffort": true,
      "thinkingFormat": "deepseek",   # kill-switch emission
      "maxTokensField": "max_tokens", # native field name
      "requiresReasoningContentOnAssistantMessages": true  # tool-scope "" net
    }

Effects after the delta:

- OFF → `thinking:{type:"disabled"}`: honored natively (off-peak) and
  rescued by the adapter to the OR OFF object (peak, 0 tokens).
- ON → `thinking:{type:"enabled"}` is added next to the root
  `reasoning_effort` (the documented native curl form); behavior is
  byte-equivalent on both routes (the adapter drops the redundant
  `thinking` on OR and keeps the cheap root effort).
- `max_tokens` replaces `max_completion_tokens` (the field DeepSeek
  documents).
- Tool scope: pi core forces `reasoning_content` (`""`; the extension
  forces `" "` where scoped — both accepted, blank-chain equivalent per
  probes).

### 9.2 Extension `pi-deepseek-reasoning-chain-fix`

Config scope (`extensions/.../config.json`) lists `deepseek/...` ids only —
the alias `litellm/deepseek-v4-flash` is NOT covered; add it to extend the
signature-restore/placeholder behavior to the alias. Role after the delta:
wire-compliance refinement (`" "` vs `""`) and signature restore for
resumed/persisted sessions (pi core replays real text only when the stored
thinking block carries a recognized signature). Its OR-turn signature
normalization is defensive insurance, not active on this route: the gateway
delivers OR reasoning as `reasoning_content` (probe 3, 2026-09-09), so pi
already stores the native signature after OR-served turns. If that drifts
to the canonical `reasoning`, native tolerates the stray field without a
4xx but drops its content (ad-hoc check 2026-09-09, n=1) and the extension
becomes the active guard.

### 9.3 Behavioral deltas at deployment

- Off-peak (alias → native): the adapter is a no-op for native-dialect
  payloads (native-bound rules only act on an OR `reasoning` object).
  models.json delta: reasoning OFF now actually disables reasoning (the
  fix); reasoning ON byte-equivalent; `reasoning_content: ""` vs LiteLLM's
  `" "` placeholder — same blank-chain class.
- Peak (alias → OR): reasoning ON → the adapter drops the redundant
  `thinking` and keeps root `reasoning_effort` (cheap on OR) —
  byte-equivalent outcome; reasoning OFF → `kill_switch_rescue` to the OR
  OFF object — 0 tokens (was 47-57 and billed).
