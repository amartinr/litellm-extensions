#!/usr/bin/env python3
"""Offline acceptance checks for reasoning_route_adapter.py (DESIGN.md section 6.1).

Stdlib only - runs in the repo venv with no network, no keys, no litellm:

    .venv/bin/python tests/test_reasoning_route_adapter.py

The module under test imports `yaml` and `litellm.integrations.custom_logger`
at module level (same layout as time_router.py), so both are stubbed in
sys.modules before the import; the real proxy provides them. ROUTE_MAP and
DIALECT_MAP are overridden with fixtures mirroring config.yaml.example:
every reasoning-capable entry declares model_info.metadata.reasoning_dialect
(the alias entry declares "openrouter" - its default deployment is OR); one
fixture entry has only a route label (fallback path) and one declares an
unrecognized dialect (fail-open path).
"""

import asyncio
import copy
import json
import os
import sys
import types
from pathlib import Path

# The module under test lives at the repo root; running this file from
# tests/ puts tests/ on sys.path, so prepend the repo root explicitly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# --------------------------------------------------------------------------- stubs
def _install_stubs():
    if "yaml" not in sys.modules:
        yaml_stub = types.ModuleType("yaml")
        yaml_stub.safe_load = lambda *a, **k: None  # unused: no config.yaml in the repo
        sys.modules["yaml"] = yaml_stub

    if "litellm" not in sys.modules:
        litellm = types.ModuleType("litellm")
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")

        class CustomLogger:
            pass

        custom_logger.CustomLogger = CustomLogger
        integrations.custom_logger = custom_logger
        litellm.integrations = integrations
        sys.modules["litellm"] = litellm
        sys.modules["litellm.integrations"] = integrations
        sys.modules["litellm.integrations.custom_logger"] = custom_logger


_install_stubs()

import reasoning_route_adapter as mod  # noqa: E402

FIXTURE_ROUTE_MAP = {
    "deepseek/deepseek-v4-flash": "deepseek",
    "deepseek/deepseek-v4-pro": "deepseek",
    "openrouter/deepseek-v4-flash": "baidu/fp8",
    "litellm/deepseek-v4-flash": "baidu/fp8",
    # entry WITHOUT reasoning_dialect -> exercises the route-label fallback
    "deepseek/legacy-no-dialect": "deepseek",
    # entry declaring an unrecognized dialect -> fail-open no-op (declared
    # dialect wins over the route label, which sits in the native set)
    "deepseek/bogus-dialect": "deepseek",
}
mod.ROUTE_MAP = dict(FIXTURE_ROUTE_MAP)

# Mirrors config.yaml.example: every reasoning-capable entry declares its
# reasoning wire contract in model_info.metadata.reasoning_dialect.
FIXTURE_DIALECT_MAP = {
    "deepseek/deepseek-v4-flash": "deepseek",
    "openrouter/deepseek-v4-flash": "openrouter",
    "litellm/deepseek-v4-flash": "openrouter",
    "deepseek/bogus-dialect": "martian",
}
mod.DIALECT_MAP = dict(FIXTURE_DIALECT_MAP)

NATIVE = "deepseek/deepseek-v4-flash"
OR_MODEL = "openrouter/deepseek-v4-flash"
ALIAS = "litellm/deepseek-v4-flash"
UNLISTED = "anthropic/claude-x"

ADAPTER = mod.proxy_handler_instance


class _Recorder:
    """Captures what the module logs via its lazy `_log()` indirection."""

    def __init__(self):
        self.lines = []

    def reset(self):
        self.lines = []

    def _emit(self, level, msg, *args):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.lines.append(f"{level}: {text}")

    def info(self, *a, **k):
        self._emit("info", *a)

    def warning(self, *a, **k):
        self._emit("warning", *a)

    def debug(self, *a, **k):
        self._emit("debug", *a)


RECORDER = _Recorder()
mod._log = lambda: RECORDER  # noqa: E731 - module-level `_log()` -> recorder


# --------------------------------------------------------------------------- harness
def _run(payload):
    return asyncio.run(ADAPTER.async_pre_call_hook(None, None, payload, "completion"))


def _dump(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _payload(model, messages=None, **extra):
    p = {"model": model, "messages": messages if messages is not None else [{"role": "user", "content": "x"}]}
    p.update(extra)
    return p


_FAILURES = []


def eq(name, got, expected):
    if got != expected:
        _FAILURES.append(f"{name}: expected {_dump(expected)}, got {_dump(got)}")


def true(name, cond, detail=""):
    if not cond:
        _FAILURES.append(f"{name}: not true {detail}")


def clear_env():
    os.environ.pop("REASONING_ADAPTER_DEBUG", None)
    os.environ.pop("REASONING_ADAPTER_DISABLED", None)
    os.environ.pop("REASONING_ADAPTER_NATIVE_ROUTES", None)
    os.environ.pop("REASONING_ADAPTER_OR_ROUTES", None)


# --------------------------------------------------------------------------- 6.1.1 classification
def test_classification():
    """6.1.1: dialect-declared classification, route-label fallback for entries
    without the declaration, fail-open on unrecognized declared dialects."""
    eq("classify native (declared dialect)",
       mod.ReasoningRouteAdapter._classify(NATIVE), ("native", "deepseek"))
    eq("classify or (declared dialect)",
       mod.ReasoningRouteAdapter._classify(OR_MODEL), ("or", "baidu/fp8"))
    eq("classify alias (declared OR dialect)",
       mod.ReasoningRouteAdapter._classify(ALIAS), ("or", "baidu/fp8"))
    eq("classify fallback via route label",
       mod.ReasoningRouteAdapter._classify("deepseek/legacy-no-dialect"),
       ("native", "deepseek"))
    eq("classify unrecognized declared dialect -> no-op",
       mod.ReasoningRouteAdapter._classify("deepseek/bogus-dialect"), None)
    eq("classify unlisted", mod.ReasoningRouteAdapter._classify(UNLISTED), None)
    eq("classify missing model", mod.ReasoningRouteAdapter._classify(None), None)
    eq("classify non-str", mod.ReasoningRouteAdapter._classify(5), None)


# --------------------------------------------------------------------------- 6.1.2 OR-bound rules
def test_or_kill_switch_rescue():
    """DESIGN 4.4 example: thinking disabled + root effort -> object OFF, byte-exact."""
    msgs = [{"role": "user", "content": "u"},
            {"role": "assistant", "content": "a", "reasoning_content": "rc",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "t"}]
    out = _run(_payload(OR_MODEL, messages=msgs,
                        thinking={"type": "disabled"}, reasoning_effort="low"))
    expected = {"model": OR_MODEL,
                "reasoning": {"enabled": False, "effort": "none"},
                "messages": msgs}
    eq("or rescue body", out, expected)
    eq("or rescue messages untouched", out["messages"], msgs)
    true("or rescue logged once",
         sum("action=kill_switch_rescue" in l for l in RECORDER.lines) == 1)


def test_or_drop_thinking_enabled():
    """DESIGN 4.4 rule 1b: thinking enabled -> drop only (never synthesize ON)."""
    out = _run(_payload(OR_MODEL, thinking={"type": "enabled"}))
    eq("or drop enabled", out, _payload(OR_MODEL))
    true("or drop logged",
         any("action=drop_thinking" in l for l in RECORDER.lines))


def test_or_curl_body_effort_preserved():
    """DeepSeek curl body on OR (thinking enabled + root effort): thinking dropped,
    root effort kept (cheap on OR, DESIGN 1.3 row 4)."""
    out = _run(_payload(OR_MODEL, thinking={"type": "enabled"}, reasoning_effort="high"))
    eq("or curl body", out, _payload(OR_MODEL, reasoning_effort="high"))


def test_or_dialect_passthrough():
    """Reasoning object (already OR) and root effort pass through byte-identical; no log."""
    for extra in ({"reasoning": {"enabled": False, "effort": "none"}},
                  {"reasoning": {"enabled": True, "effort": "low"}},
                  {"reasoning_effort": "low"},
                  {}):
        before = _payload(OR_MODEL, **extra)
        snapshot = _dump(before)
        out = _run(before)
        eq(f"or passthrough {extra}", _dump(out), snapshot)
    eq("or passthrough no log", RECORDER.lines, [])


# --------------------------------------------------------------------------- 6.1.3 native-bound rules
def test_native_object_off():
    """DESIGN 4.5 example: object OFF -> thinking disabled, byte-exact."""
    msgs = [{"role": "user", "content": "u"}]
    out = _run(_payload(NATIVE, messages=msgs,
                        reasoning={"enabled": False, "effort": "none"}))
    expected = {"model": NATIVE, "thinking": {"type": "disabled"}, "messages": msgs}
    eq("native off body", out, expected)
    eq("native off messages untouched", out["messages"], msgs)
    true("native off logged", any("action=object_to_native_off" in l for l in RECORDER.lines))

    # OFF also when effort is "none" even with enabled true.
    out2 = _run(_payload(NATIVE, reasoning={"enabled": True, "effort": "none"}))
    eq("native off via effort none", out2["thinking"], {"type": "disabled"})
    true("native off via effort none no reasoning key", "reasoning" not in out2)


def test_native_object_on_vocabulary():
    """DESIGN 4.5 vocabulary table rows."""
    for effort, mapped in (("low", "low"), ("high", "high"), ("max", "max"),
                           ("medium", "high"), ("xhigh", "high"), ("minimal", "low")):
        out = _run(_payload(NATIVE, reasoning={"enabled": True, "effort": effort}))
        eq(f"native on {effort}", out,
           _payload(NATIVE, thinking={"type": "enabled"}, reasoning_effort=mapped))
        true(f"native on {effort} reasoning removed", "reasoning" not in out)


def test_native_object_on_unmappable():
    """Unmappable effort (string or otherwise) -> thinking enabled only, effort key omitted."""
    for extra in ({"reasoning": {"enabled": True, "effort": "bogus"}},
                  {"reasoning": {"enabled": True, "effort": 5}},
                  {"reasoning": {"enabled": True, "effort": {"x": 1}}},
                  {"reasoning": {"enabled": True}}):
        out = _run(_payload(NATIVE, **extra))
        eq(f"native on unmappable {extra}", out,
           _payload(NATIVE, thinking={"type": "enabled"}))


def test_native_dialect_passthrough():
    """Native dialect (incl. the DeepSeek curl body: thinking + root effort together)
    passes through byte-identical on both routes."""
    for model in (NATIVE, OR_MODEL):
        for extra in ({"thinking": {"type": "disabled"}},
                      {"reasoning_effort": "high"},
                      {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
                      {}):
            # thinking on the OR route is normalized (rescue/drop), not passthrough:
            if model == OR_MODEL and "thinking" in extra:
                continue
            before = _payload(model, **extra)
            snapshot = _dump(before)
            out = _run(before)
            eq(f"native dialect passthrough {model} {extra}", _dump(out), snapshot)
    eq("passthrough no log", RECORDER.lines, [])


# --------------------------------------------------------------------------- 6.1.4 idempotency
def test_idempotency():
    """Applying twice -> second run is a no-op: unchanged payload, no log."""
    for model, extra in ((OR_MODEL, {"thinking": {"type": "disabled"}, "reasoning_effort": "low"}),
                         (NATIVE, {"reasoning": {"enabled": False, "effort": "none"}}),
                         (NATIVE, {"reasoning": {"enabled": True, "effort": "medium"}})):
        p = _payload(model, **extra)
        _run(p)
        first = _dump(p)
        RECORDER.reset()
        _run(p)
        eq(f"idempotent body {model} {extra}", _dump(p), first)
        eq(f"idempotent no log {model} {extra}", RECORDER.lines, [])


# --------------------------------------------------------------------------- 6.1.5 fail-open
def test_fail_open_malformed():
    """Malformed values -> unchanged data, no exception, no log."""
    for model, extra in ((OR_MODEL, {"thinking": "junk"}),
                         (OR_MODEL, {"thinking": None}),
                         (OR_MODEL, {"thinking": ["disabled"]}),
                         (NATIVE, {"reasoning": "junk"}),
                         (NATIVE, {"reasoning": {"enabled": "yes"}}),       # not a bool
                         (NATIVE, {"reasoning": {"effort": "low"}})):      # enabled absent
        before = _payload(model, **extra)
        snapshot = _dump(before)
        out = _run(before)
        eq(f"malformed unchanged {model} {extra}", _dump(out), snapshot)
        eq(f"malformed no log {model} {extra}", RECORDER.lines, [])


def test_fail_open_exception():
    """Non-dict data -> exception caught, data returned unchanged, warning emitted."""
    out = _run(None)
    true("data None returned as-is", out is None)
    true("warning emitted", any(l.startswith("warning:") for l in RECORDER.lines))


# --------------------------------------------------------------------------- 6.1.6/7 misc
def test_unlisted_model_noop():
    p = _payload(UNLISTED, thinking={"type": "disabled"}, reasoning={"enabled": False})
    snapshot = _dump(p)
    out = _run(p)
    eq("unlisted unchanged", _dump(out), snapshot)
    eq("unlisted no log", RECORDER.lines, [])


def test_messages_never_touched():
    msgs = [{"role": "user", "content": "u"},
            {"role": "assistant", "content": "a", "reasoning_content": "rc",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "t"}]
    cases = [
        _payload(OR_MODEL, messages=msgs, thinking={"type": "disabled"}),
        _payload(OR_MODEL, messages=msgs, reasoning={"enabled": True, "effort": "low"}),
        _payload(NATIVE, messages=msgs, reasoning={"enabled": False, "effort": "none"}),
        _payload(NATIVE, messages=msgs, reasoning={"enabled": True, "effort": "medium"}),
        _payload(NATIVE, messages=msgs, thinking={"type": "disabled"}, reasoning_effort="low"),
        _payload(OR_MODEL, messages=msgs),
    ]
    for p in cases:
        orig = copy.deepcopy(p["messages"])
        _run(p)
        eq(f"messages untouched {sorted(p.keys())}", p["messages"], orig)


def test_disabled_env():
    os.environ["REASONING_ADAPTER_DISABLED"] = "1"
    try:
        p = _payload(OR_MODEL, thinking={"type": "disabled"})
        snapshot = _dump(p)
        out = _run(p)
        eq("disabled env unchanged", _dump(out), snapshot)
        eq("disabled env no log", RECORDER.lines, [])
    finally:
        clear_env()


def test_debug_env_logs_before_after():
    os.environ["REASONING_ADAPTER_DEBUG"] = "1"
    try:
        p = _payload(OR_MODEL, thinking={"type": "disabled"}, reasoning_effort="low")
        _run(p)
        true("debug before/after line",
             any("before=" in l and "after=" in l for l in RECORDER.lines))
    finally:
        clear_env()


TESTS = [
    test_classification,
    test_or_kill_switch_rescue,
    test_or_drop_thinking_enabled,
    test_or_curl_body_effort_preserved,
    test_or_dialect_passthrough,
    test_native_object_off,
    test_native_object_on_vocabulary,
    test_native_object_on_unmappable,
    test_native_dialect_passthrough,
    test_idempotency,
    test_fail_open_malformed,
    test_fail_open_exception,
    test_unlisted_model_noop,
    test_messages_never_touched,
    test_disabled_env,
    test_debug_env_logs_before_after,
]


def main():
    clear_env()
    RECORDER.reset()
    failed = 0
    for t in TESTS:
        RECORDER.reset()
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc!r}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests ok")
    if _FAILURES:
        print(f"{len(_FAILURES)} assertion failure(s):")
        for f in _FAILURES:
            print("  -", f)
    sys.exit(1 if (failed or _FAILURES) else 0)


if __name__ == "__main__":
    main()
