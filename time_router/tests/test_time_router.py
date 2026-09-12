"""Tests for time_router — config-driven routing, stickiness, labeling, fail-open."""

import asyncio
import time
from datetime import datetime, timezone

import pytest
import yaml

import hook_config
import time_router

ALIAS = "litellm/deepseek-v4-flash"
PEAK = "openrouter/deepseek-v4-flash"
OFFPEAK = "deepseek/deepseek-v4-flash"
WINDOWS = [
    {"days": [0, 1, 2, 3, 4], "start": "01:00", "end": "04:00"},
    {"days": [0, 1, 2, 3, 4], "start": "06:00", "end": "10:00"},
]


def _now(hour, minute=0, weekday=0) -> datetime:
    # 2026-01-05 is a Monday; +weekday keeps the date valid for 0..6.
    return datetime(2026, 1, 5 + weekday, hour, minute, tzinfo=timezone.utc)


def _route(peak=PEAK, offpeak=OFFPEAK, windows=WINDOWS) -> time_router.AliasRoute:
    return time_router.AliasRoute(
        peak_target=peak,
        offpeak_target=offpeak,
        windows=tuple(hook_config.parse_window(w) for w in windows),
    )


def _run(data):
    return asyncio.run(time_router.proxy_handler_instance.async_pre_call_hook(None, None, data, "completion"))


@pytest.fixture(autouse=True)
def _clean_state():
    time_router._session_state.clear()
    yield
    time_router._session_state.clear()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(time_router, "ALIASES", {ALIAS: _route()})
    monkeypatch.setattr(time_router, "ROUTE_MAP", {PEAK: "baidu/fp8", OFFPEAK: "deepseek"})
    monkeypatch.setattr(time_router, "SESSION_TTL_S", 900)
    monkeypatch.setattr(time_router, "MAX_SESSION_ENTRIES", 128)
    return ALIAS


@pytest.fixture
def restore_config():
    saved = (
        time_router.ROUTE_MAP,
        time_router.ALIASES,
        time_router.SESSION_TTL_S,
        time_router.MAX_SESSION_ENTRIES,
    )
    yield
    (
        time_router.ROUTE_MAP,
        time_router.ALIASES,
        time_router.SESSION_TTL_S,
        time_router.MAX_SESSION_ENTRIES,
    ) = saved


# --------------------------------------------------------------- schedule (data-driven)
@pytest.mark.parametrize(
    "hour,minute,weekday,expected",
    [
        (0, 59, 0, False),   # before the first window
        (1, 0, 0, True),     # start inclusive
        (3, 59, 0, True),
        (4, 0, 0, False),    # end exclusive
        (5, 59, 0, False),   # gap between windows
        (6, 0, 0, True),
        (9, 59, 0, True),
        (10, 0, 0, False),
        (2, 0, 4, True),     # Friday is in days
        (2, 0, 5, False),    # Saturday omitted -> off-peak
        (2, 0, 6, False),    # Sunday omitted
    ],
)
def test_is_peak(hour, minute, weekday, expected):
    assert _route().is_peak(_now(hour, minute, weekday)) is expected


def test_target_at_uses_windows():
    route = _route()
    assert route.target_at(_now(2)) == PEAK
    assert route.target_at(_now(5)) == OFFPEAK
    assert route.target_at(_now(2, weekday=5)) == OFFPEAK


# ----------------------------------------------------------------------- stickiness
def test_pick_route_no_session_is_stateless(configured):
    route = _route()
    handler = time_router.TimeRouter()
    assert handler._pick_route(ALIAS, route, None, _now(2)) == PEAK
    assert handler._pick_route(ALIAS, route, None, _now(5)) == OFFPEAK


def test_pick_route_new_session_follows_clock(configured):
    handler = time_router.TimeRouter()
    assert handler._pick_route(ALIAS, _route(), "s1", _now(2)) == PEAK


def test_pick_route_openrouter_ratchet(configured):
    handler = time_router.TimeRouter()
    time_router._session_state["s1"] = {"alias": ALIAS, "route": PEAK, "last_seen": time.time()}
    # pinned to peak, now off-peak -> stays
    assert handler._pick_route(ALIAS, _route(), "s1", _now(5)) == PEAK


def test_pick_route_direct_switches_to_peak(configured):
    handler = time_router.TimeRouter()
    time_router._session_state["s1"] = {"alias": ALIAS, "route": OFFPEAK, "last_seen": time.time()}
    assert handler._pick_route(ALIAS, _route(), "s1", _now(2)) == PEAK


def test_pick_route_idle_expiry(configured, monkeypatch):
    monkeypatch.setattr(time_router, "SESSION_TTL_S", 10)
    handler = time_router.TimeRouter()
    time_router._session_state["s1"] = {"alias": ALIAS, "route": PEAK, "last_seen": time.time() - 100}
    assert handler._pick_route(ALIAS, _route(), "s1", _now(5)) == OFFPEAK


def test_pick_route_other_alias_resets(configured):
    handler = time_router.TimeRouter()
    time_router._session_state["s1"] = {"alias": "other/alias", "route": PEAK, "last_seen": time.time()}
    assert handler._pick_route(ALIAS, _route(), "s1", _now(5)) == OFFPEAK


# --------------------------------------------------------------------------- pruning
def test_prune_evicts_oldest_when_over_cap(monkeypatch):
    monkeypatch.setattr(time_router, "MAX_SESSION_ENTRIES", 2)
    monkeypatch.setattr(time_router, "SESSION_TTL_S", 10_000)
    now = time.time()
    time_router._session_state.update(
        {
            "old": {"alias": ALIAS, "route": PEAK, "last_seen": now - 100},
            "mid": {"alias": ALIAS, "route": PEAK, "last_seen": now - 50},
            "new": {"alias": ALIAS, "route": PEAK, "last_seen": now},
        }
    )
    time_router.TimeRouter._prune_sessions(now)
    assert set(time_router._session_state) == {"mid", "new"}


def test_prune_drops_expired(monkeypatch):
    monkeypatch.setattr(time_router, "MAX_SESSION_ENTRIES", 1)
    monkeypatch.setattr(time_router, "SESSION_TTL_S", 10)
    now = time.time()
    time_router._session_state.update(
        {
            "expired": {"alias": ALIAS, "route": PEAK, "last_seen": now - 100},
            "fresh": {"alias": ALIAS, "route": PEAK, "last_seen": now},
        }
    )
    time_router.TimeRouter._prune_sessions(now)
    assert set(time_router._session_state) == {"fresh"}


# ------------------------------------------------------------------------ injection
def test_inject_route_writes_all_three(configured):
    data = {}
    time_router.TimeRouter._inject_route(data, "deepseek")
    assert data["metadata"]["route"] == "deepseek"
    assert data["metadata"]["requester_metadata"]["route"] == "deepseek"
    assert data["metadata"]["spend_logs_metadata"]["route"] == "deepseek"


# ------------------------------------------------------------------------- clock
def test_utc_now_honors_fakes(monkeypatch):
    monkeypatch.setenv("TIME_ROUTER_FAKE_WEEKDAY", "5")
    monkeypatch.setenv("TIME_ROUTER_FAKE_HOUR", "2")
    monkeypatch.setenv("TIME_ROUTER_FAKE_MINUTE", "30")
    now = time_router._utc_now()
    assert now.weekday() == 5
    assert (now.hour, now.minute) == (2, 30)


def test_utc_now_invalid_fake_ignored(monkeypatch):
    monkeypatch.setenv("TIME_ROUTER_FAKE_HOUR", "abc")
    monkeypatch.delenv("TIME_ROUTER_FAKE_WEEKDAY", raising=False)
    assert isinstance(time_router._utc_now(), datetime)


# ------------------------------------------------------------- hook (integration)
def test_alias_reroutes_at_peak(configured, monkeypatch):
    monkeypatch.setattr(time_router, "_utc_now", lambda: _now(2))
    out = _run({"model": ALIAS})
    assert out["model"] == PEAK
    assert out["metadata"]["route"] == "baidu/fp8"
    assert out["metadata"]["requester_metadata"]["route"] == "baidu/fp8"
    assert out["metadata"]["spend_logs_metadata"]["route"] == "baidu/fp8"


def test_alias_reroutes_offpeak(configured, monkeypatch):
    monkeypatch.setattr(time_router, "_utc_now", lambda: _now(5))
    out = _run({"model": ALIAS})
    assert out["model"] == OFFPEAK
    assert out["metadata"]["route"] == "deepseek"


def test_hook_sticky_keeps_peak_target(configured, monkeypatch):
    state = {"now": _now(2)}
    monkeypatch.setattr(time_router, "_utc_now", lambda: state["now"])
    _run({"model": ALIAS, "metadata": {"session_id": "s1"}})
    state["now"] = _now(5)
    out = _run({"model": ALIAS, "metadata": {"session_id": "s1"}})
    assert out["model"] == PEAK


def test_direct_model_is_labeled_not_rerouted(configured):
    out = _run({"model": OFFPEAK})
    assert out["model"] == OFFPEAK
    assert out["metadata"]["route"] == "deepseek"


def test_unlisted_model_is_untouched(configured):
    data = {"model": "anthropic/claude-x"}
    out = _run(data)
    assert out == {"model": "anthropic/claude-x"}


def test_hook_fail_open_on_bad_metadata(configured, monkeypatch):
    monkeypatch.setattr(time_router, "_utc_now", lambda: _now(2))
    data = {"model": ALIAS, "metadata": "junk"}
    out = _run(data)  # must not raise
    assert out is data
    assert out["metadata"] == "junk"


def test_non_string_session_id_is_ignored(configured, monkeypatch):
    monkeypatch.setattr(time_router, "_utc_now", lambda: _now(2))
    out = _run({"model": ALIAS, "metadata": {"session_id": 123}})
    assert out["model"] == PEAK
    assert time_router._session_state == {}


# ------------------------------------------------------------- config wiring
def test_build_aliases_from_models():
    models = {
        ALIAS: {"time_router": {"reroute": {"peak_target": PEAK, "offpeak_target": OFFPEAK}}},
        OFFPEAK: {"time_router": {"peak_windows": WINDOWS}},
    }
    aliases = time_router._build_aliases(models)
    assert set(aliases) == {ALIAS}
    assert aliases[ALIAS].peak_target == PEAK
    assert len(aliases[ALIAS].windows) == 2


def _write_config(tmp_path, **overrides):
    cfg = {
        "model_list": [
            {
                "model_name": ALIAS,
                "model_info": {
                    "metadata": {
                        "route": "baidu/fp8",
                        "reasoning_dialect": "openrouter",
                        "time_router": {
                            "reroute": {"peak_target": PEAK, "offpeak_target": OFFPEAK}
                        },
                    }
                },
            },
            {
                "model_name": OFFPEAK,
                "model_info": {
                    "metadata": {
                        "route": "deepseek",
                        "reasoning_dialect": "deepseek",
                        "time_router": {"peak_windows": WINDOWS},
                    }
                },
            },
            {"model_name": PEAK, "model_info": {"metadata": {"route": "baidu/fp8"}}},
        ],
        "callback_settings": {"time_router": {"session_ttl_s": 42, "max_session_entries": 7}},
    }
    cfg.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_load_config_wires_targets_windows_and_knobs(tmp_path, restore_config):
    time_router._load_config(str(_write_config(tmp_path)))
    assert set(time_router.ALIASES) == {ALIAS}
    assert time_router.ALIASES[ALIAS].target_at(_now(2)) == PEAK
    assert time_router.ROUTE_MAP[OFFPEAK] == "deepseek"
    assert time_router.SESSION_TTL_S == 42
    assert time_router.MAX_SESSION_ENTRIES == 7


def test_load_config_renamed_target_follows_config(tmp_path, restore_config):
    renamed = "openrouter/renamed"
    path = _write_config(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["model_list"][0]["model_info"]["metadata"]["time_router"]["reroute"]["peak_target"] = renamed
    raw["model_list"][2]["model_name"] = renamed
    path.write_text(yaml.safe_dump(raw))

    time_router._load_config(str(path))
    assert time_router.ALIASES[ALIAS].target_at(_now(2)) == renamed


def test_load_config_invalid_target_disables_reroute(tmp_path, restore_config):
    path = _write_config(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["model_list"][0]["model_info"]["metadata"]["time_router"]["reroute"]["peak_target"] = "missing"
    path.write_text(yaml.safe_dump(raw))

    time_router._load_config(str(path))
    assert time_router.ALIASES == {}
