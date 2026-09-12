"""Tests for reasoning_route_adapter — per-deployment reasoning-dialect normalization."""

import asyncio
import copy

import pytest
import yaml

import reasoning_route_adapter as adapter

NATIVE = "deepseek/deepseek-v4-flash"
OR = "openrouter/deepseek-v4-flash"
ALIAS = "litellm/deepseek-v4-flash"
UNLISTED = "anthropic/claude-x"

HANDLER = adapter.proxy_handler_instance


def _kwargs(deployment, dialect=None, route=None, bucket="metadata", **extra):
    meta = {}
    if dialect is not None:
        meta["reasoning_dialect"] = dialect
    if route is not None:
        meta["route"] = route
    payload = {
        "model": f"{deployment}-provider",
        bucket: {"deployment_model_name": deployment, "model_info": {"metadata": meta}},
    }
    payload.update(extra)
    return payload


def _classify(kwargs):
    return HANDLER._classify(adapter.deployment_context(kwargs))


def _run(kwargs):
    return asyncio.run(HANDLER.async_pre_call_deployment_hook(kwargs, None))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("REASONING_ADAPTER_DISABLED", raising=False)
    monkeypatch.delenv("REASONING_ADAPTER_DEBUG", raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(adapter, "ROUTE_MAP", {NATIVE: "deepseek", OR: "baidu/fp8", ALIAS: "baidu/fp8"})
    monkeypatch.setattr(adapter, "DIALECT_MAP", {NATIVE: "deepseek", OR: "openrouter", ALIAS: "openrouter"})
    monkeypatch.setattr(adapter, "NATIVE_ROUTE_LABELS", {"deepseek"})
    monkeypatch.setattr(adapter, "OR_ROUTE_LABELS", {"baidu/fp8"})
    return adapter


@pytest.fixture
def restore_config():
    saved = (
        adapter.ROUTE_MAP,
        adapter.DIALECT_MAP,
        adapter.NATIVE_ROUTE_LABELS,
        adapter.OR_ROUTE_LABELS,
        adapter._WARNING_INTERVAL_S,
    )
    yield
    (
        adapter.ROUTE_MAP,
        adapter.DIALECT_MAP,
        adapter.NATIVE_ROUTE_LABELS,
        adapter.OR_ROUTE_LABELS,
        adapter._WARNING_INTERVAL_S,
    ) = saved


# ------------------------------------------------------------------ entry point
def test_migrated_to_the_deployment_hook():
    assert "async_pre_call_deployment_hook" in adapter.ReasoningRouteAdapter.__dict__
    assert "async_pre_call_hook" not in adapter.ReasoningRouteAdapter.__dict__


# ------------------------------------------------------------------ classification
def test_declared_dialect_wins(configured):
    assert _classify(_kwargs(NATIVE, dialect="deepseek"))[0] == "native"
    assert _classify(_kwargs(OR, dialect="openrouter"))[0] == "or"
    assert _classify(_kwargs(ALIAS, dialect="openrouter"))[0] == "or"


def test_label_prefers_declared_route(configured):
    side, label = _classify(_kwargs(OR, dialect="openrouter", route="baidu/fp8"))
    assert (side, label) == ("or", "baidu/fp8")


def test_fallback_to_dialect_map(configured):
    # No reasoning_dialect in model_info -> DIALECT_MAP[deployment_model_name]
    assert _classify(_kwargs(NATIVE))[0] == "native"
    assert _classify(_kwargs(OR))[0] == "or"


def test_fallback_to_route_label_taxonomy(configured, monkeypatch):
    monkeypatch.setattr(adapter, "DIALECT_MAP", {})
    monkeypatch.setattr(adapter, "ROUTE_MAP", {"legacy/no-dialect": "deepseek"})
    assert _classify(_kwargs("legacy/no-dialect"))[0] == "native"


def test_unrecognized_declared_dialect_is_none(configured):
    assert _classify(_kwargs(NATIVE, dialect="martian")) is None


def test_unlisted_deployment_is_none(configured):
    assert _classify(_kwargs(UNLISTED)) is None


def test_no_deployment_metadata_is_none(configured):
    assert _classify({"model": "x"}) is None


def test_litellm_metadata_bucket_is_read(configured):
    kwargs = _kwargs(OR, dialect="openrouter", bucket="litellm_metadata")
    assert _classify(kwargs)[0] == "or"


# ------------------------------------------------------------------ OR-bound rules
def test_or_kill_switch_rescue(configured):
    kwargs = _kwargs(OR, dialect="openrouter", thinking={"type": "disabled"}, reasoning_effort="low")
    out = _run(kwargs)
    assert out["reasoning"] == {"enabled": False, "effort": "none"}
    assert "thinking" not in out
    assert "reasoning_effort" not in out


def test_or_drop_thinking_enabled(configured):
    kwargs = _kwargs(OR, dialect="openrouter", thinking={"type": "enabled"})
    out = _run(kwargs)
    assert "thinking" not in out
    assert "reasoning" not in out


def test_or_keeps_root_effort(configured):
    kwargs = _kwargs(OR, dialect="openrouter", thinking={"type": "enabled"}, reasoning_effort="high")
    out = _run(kwargs)
    assert "thinking" not in out
    assert out["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "extra",
    [
        {"reasoning": {"enabled": False, "effort": "none"}},
        {"reasoning": {"enabled": True, "effort": "low"}},
        {"reasoning_effort": "low"},
        {},
    ],
)
def test_or_dialect_passthrough(configured, extra):
    kwargs = _kwargs(OR, dialect="openrouter", **extra)
    snapshot = copy.deepcopy(kwargs)
    assert _run(kwargs) == snapshot


# -------------------------------------------------------------- native-bound rules
def test_native_object_off(configured):
    kwargs = _kwargs(NATIVE, dialect="deepseek", reasoning={"enabled": False, "effort": "none"})
    out = _run(kwargs)
    assert out["thinking"] == {"type": "disabled"}
    assert "reasoning" not in out


@pytest.mark.parametrize(
    "effort,mapped",
    [("low", "low"), ("high", "high"), ("max", "max"), ("medium", "high"), ("xhigh", "high"), ("minimal", "low")],
)
def test_native_object_on_vocabulary(configured, effort, mapped):
    kwargs = _kwargs(NATIVE, dialect="deepseek", reasoning={"enabled": True, "effort": effort})
    out = _run(kwargs)
    assert out["thinking"] == {"type": "enabled"}
    assert out["reasoning_effort"] == mapped
    assert "reasoning" not in out


@pytest.mark.parametrize("effort", ["bogus", 5, None])
def test_native_object_on_unmappable_omits_effort(configured, effort):
    kwargs = _kwargs(NATIVE, dialect="deepseek", reasoning={"enabled": True, "effort": effort})
    out = _run(kwargs)
    assert out["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in out


def test_native_dialect_passthrough(configured):
    kwargs = _kwargs(NATIVE, dialect="deepseek", thinking={"type": "disabled"}, reasoning_effort="high")
    snapshot = copy.deepcopy(kwargs)
    assert _run(kwargs) == snapshot


# ------------------------------------------------------------------ idempotency
def test_idempotent(configured):
    payloads = [
        _kwargs(OR, dialect="openrouter", thinking={"type": "disabled"}, reasoning_effort="low"),
        _kwargs(NATIVE, dialect="deepseek", reasoning={"enabled": False, "effort": "none"}),
        _kwargs(NATIVE, dialect="deepseek", reasoning={"enabled": True, "effort": "medium"}),
    ]
    for kwargs in payloads:
        _run(kwargs)
        after_first = copy.deepcopy(kwargs)
        _run(kwargs)
        assert kwargs == after_first


# ------------------------------------------------------- fallback re-normalization
def test_fallback_attempt_renormalizes(configured):
    """The same kwargs is reused across attempts; the second (fallback) attempt
    must rescue the native kill switch against the OR deployment."""
    kwargs = _kwargs(NATIVE, dialect="deepseek", thinking={"type": "disabled"})
    _run(kwargs)
    assert kwargs["thinking"] == {"type": "disabled"}  # native attempt: passthrough

    # Router swaps the bound deployment for the fallback attempt.
    kwargs["metadata"] = {
        "deployment_model_name": OR,
        "model_info": {"metadata": {"reasoning_dialect": "openrouter", "route": "baidu/fp8"}},
    }
    _run(kwargs)
    assert kwargs["reasoning"] == {"enabled": False, "effort": "none"}
    assert "thinking" not in kwargs


# ------------------------------------------------------------------ robustness
def test_messages_never_touched(configured):
    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "reasoning_content": "rc"},
        {"role": "tool", "tool_call_id": "c1", "content": "t"},
    ]
    kwargs = _kwargs(OR, dialect="openrouter", messages=msgs, thinking={"type": "disabled"})
    original = copy.deepcopy(msgs)
    _run(kwargs)
    assert kwargs["messages"] == original


def test_disabled_env_leaves_request_unchanged(configured, monkeypatch):
    monkeypatch.setenv("REASONING_ADAPTER_DISABLED", "1")
    kwargs = _kwargs(OR, dialect="openrouter", thinking={"type": "disabled"})
    snapshot = copy.deepcopy(kwargs)
    assert _run(kwargs) == snapshot


@pytest.mark.parametrize(
    "extra",
    [
        {"thinking": "junk"},
        {"thinking": None},
        {"reasoning": "junk"},
        {"reasoning": {"enabled": "yes"}},
    ],
)
def test_fail_open_malformed(configured, extra):
    kwargs = _kwargs(OR, dialect="openrouter", **extra)
    snapshot = copy.deepcopy(kwargs)
    assert _run(kwargs) == snapshot


def test_non_dict_kwargs_returns_as_is(configured):
    assert _run(None) is None


# ------------------------------------------------------------------ config wiring
def _write_config(tmp_path, **overrides):
    cfg = {
        "model_list": [
            {"model_name": NATIVE, "model_info": {"metadata": {"route": "deepseek", "reasoning_dialect": "deepseek"}}},
            {"model_name": OR, "model_info": {"metadata": {"route": "baidu/fp8", "reasoning_dialect": "openrouter"}}},
            {"model_name": ALIAS, "model_info": {"metadata": {"route": "baidu/fp8", "reasoning_dialect": "openrouter"}}},
        ],
        "callback_settings": {
            "reasoning_route_adapter": {
                "warning_interval_s": 7,
                "native_route_labels": ["deepseek"],
                "or_route_labels": ["baidu/fp8"],
            }
        },
    }
    cfg.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_load_config_wires_maps_and_knobs(tmp_path, restore_config):
    adapter._load_config(str(_write_config(tmp_path)))
    assert adapter.ROUTE_MAP[NATIVE] == "deepseek"
    assert adapter.DIALECT_MAP[OR] == "openrouter"
    assert adapter.NATIVE_ROUTE_LABELS == {"deepseek"}
    assert adapter.OR_ROUTE_LABELS == {"baidu/fp8"}
    assert adapter._WARNING_INTERVAL_S == 7


def test_load_config_defaults_when_block_absent(tmp_path, restore_config):
    path = _write_config(tmp_path, callback_settings={})
    adapter._load_config(str(path))
    assert adapter._WARNING_INTERVAL_S == 300
    assert adapter.NATIVE_ROUTE_LABELS == {"deepseek"}
    assert adapter.OR_ROUTE_LABELS == {"baidu/fp8"}
