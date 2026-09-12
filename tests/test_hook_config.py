"""Tests for hook_config — the shared configuration loader."""

import pytest

import hook_config


def _entry(name, **metadata):
    return {"model_name": name, "model_info": {"metadata": metadata}}


@pytest.fixture
def valid_config():
    return {
        "model_list": [
            _entry(
                "litellm/deepseek-v4-flash",
                route="baidu/fp8",
                reasoning_dialect="openrouter",
                time_router={
                    "reroute": {
                        "peak_target": "openrouter/deepseek-v4-flash",
                        "offpeak_target": "deepseek/deepseek-v4-flash",
                    }
                },
            ),
            _entry(
                "deepseek/deepseek-v4-flash",
                route="deepseek",
                reasoning_dialect="deepseek",
                time_router={
                    "peak_windows": [
                        {"days": [0, 1, 2, 3, 4], "start": "01:00", "end": "04:00"},
                        {"days": [0, 1, 2, 3, 4], "start": "06:00", "end": "10:00"},
                    ]
                },
            ),
            _entry("openrouter/deepseek-v4-flash", route="baidu/fp8", reasoning_dialect="openrouter"),
        ],
        "callback_settings": {"time_router": {"session_ttl_s": 60, "max_session_entries": 8}},
    }


@pytest.fixture
def logs(monkeypatch):
    """Capture hook_config log calls instead of emitting them."""

    class Recorder:
        def __init__(self):
            self.messages = []

        def _record(self, level, msg, *args):
            try:
                text = msg % args if args else str(msg)
            except Exception:
                text = str(msg)
            self.messages.append((level, text))

        def error(self, msg, *args):
            self._record("error", msg, *args)

        def warning(self, msg, *args):
            self._record("warning", msg, *args)

        def info(self, msg, *args):
            self._record("info", msg, *args)

    recorder = Recorder()
    monkeypatch.setattr(hook_config, "get_logger", lambda: recorder)
    return recorder


# --------------------------------------------------------------------- parsing
def test_parse_config_builds_descriptors(valid_config):
    models, _ = hook_config.parse_config(valid_config)

    assert set(models) == {
        "litellm/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
        "openrouter/deepseek-v4-flash",
    }
    assert models["deepseek/deepseek-v4-flash"]["route"] == "deepseek"
    assert models["deepseek/deepseek-v4-flash"]["reasoning_dialect"] == "deepseek"
    assert models["litellm/deepseek-v4-flash"]["time_router"]["reroute"]["peak_target"] == (
        "openrouter/deepseek-v4-flash"
    )


def test_parse_config_skips_entries_without_name(valid_config):
    valid_config["model_list"].append({"model_info": {"metadata": {"route": "x"}}})
    models, _ = hook_config.parse_config(valid_config)
    assert "x" not in {d["route"] for d in models.values()}


def test_parse_config_reads_callback_settings(valid_config):
    _, raw = hook_config.parse_config(valid_config)
    assert raw == {"time_router": {"session_ttl_s": 60, "max_session_entries": 8}}


def test_parse_config_non_dict_callback_settings(valid_config):
    valid_config["callback_settings"] = True
    _, raw = hook_config.parse_config(valid_config)
    assert raw == {}


def test_parse_config_valid_reroute_is_kept(valid_config, logs):
    models, _ = hook_config.parse_config(valid_config)
    assert models["litellm/deepseek-v4-flash"]["time_router"]["reroute"]
    assert logs.messages == []


# ------------------------------------------------------------------ validation
def test_missing_peak_target_disables_reroute(valid_config, logs):
    valid_config["model_list"][0]["model_info"]["metadata"]["time_router"]["reroute"]["peak_target"] = "nope"
    models, _ = hook_config.parse_config(valid_config)

    assert "reroute" not in models["litellm/deepseek-v4-flash"]["time_router"]
    assert any(level == "error" and "peak_target" in text for level, text in logs.messages)


def test_missing_offpeak_target_disables_reroute(valid_config, logs):
    valid_config["model_list"][0]["model_info"]["metadata"]["time_router"]["reroute"]["offpeak_target"] = "nope"
    models, _ = hook_config.parse_config(valid_config)

    assert "reroute" not in models["litellm/deepseek-v4-flash"]["time_router"]
    assert any(level == "error" and "offpeak_target" in text for level, text in logs.messages)


def test_offpeak_without_windows_is_an_error(valid_config, logs):
    del valid_config["model_list"][1]["model_info"]["metadata"]["time_router"]["peak_windows"]
    models, _ = hook_config.parse_config(valid_config)

    assert "reroute" not in models["litellm/deepseek-v4-flash"]["time_router"]
    assert any(level == "error" and "peak_windows" in text for level, text in logs.messages)


@pytest.mark.parametrize(
    "window",
    [
        {"days": [], "start": "01:00", "end": "04:00"},          # no days
        {"days": [7], "start": "01:00", "end": "04:00"},          # out of range
        {"days": [-1], "start": "01:00", "end": "04:00"},         # out of range
        {"days": [0], "start": "25:00", "end": "04:00"},          # bad hour
        {"days": [0], "start": "abc", "end": "04:00"},            # not a time
        {"days": [0], "start": "04:00", "end": "04:00"},          # zero length
        {"days": [0], "start": "05:00", "end": "04:00"},          # inverted
        {"days": [0], "start": "01:00"},                          # missing end
    ],
)
def test_invalid_window_disables_reroute(valid_config, logs, window):
    valid_config["model_list"][1]["model_info"]["metadata"]["time_router"]["peak_windows"] = [window]
    models, _ = hook_config.parse_config(valid_config)

    assert "reroute" not in models["litellm/deepseek-v4-flash"]["time_router"]
    assert any(level == "error" and "invalid peak window" in text for level, text in logs.messages)


# ---------------------------------------------------------------- parse_window
def test_parse_window_valid():
    days, start, end = hook_config.parse_window({"days": [0, 1, 2, 3, 4], "start": "01:00", "end": "04:00"})
    assert days == frozenset({0, 1, 2, 3, 4})
    assert (start.hour, start.minute) == (1, 0)
    assert (end.hour, end.minute) == (4, 0)


def test_parse_window_weekend_excluded_by_omission():
    days, _, _ = hook_config.parse_window({"days": [0, 1, 2, 3, 4], "start": "01:00", "end": "04:00"})
    assert 5 not in days and 6 not in days


# -------------------------------------------------------------------- settings
def test_settings_for_defaults_when_absent():
    assert hook_config.settings_for({}, "time_router") == {
        "session_ttl_s": 900,
        "max_session_entries": 128,
    }
    assert hook_config.settings_for({}, "reasoning_route_adapter") == {
        "warning_interval_s": 300,
        "native_route_labels": ["deepseek"],
        "or_route_labels": ["baidu/fp8"],
    }


def test_settings_for_overrides():
    raw = {"time_router": {"session_ttl_s": 60}}
    settings = hook_config.settings_for(raw, "time_router")
    assert settings["session_ttl_s"] == 60
    assert settings["max_session_entries"] == 128  # untouched default


def test_settings_for_unknown_hook_is_empty():
    assert hook_config.settings_for({}, "nope") == {}


@pytest.mark.parametrize(
    "block",
    [
        {"session_ttl_s": "60"},
        {"session_ttl_s": True},           # bool is not an int knob
        {"max_session_entries": 1.5},
    ],
)
def test_settings_for_wrong_type_falls_back_to_default(logs, block):
    settings = hook_config.settings_for({"time_router": block}, "time_router")
    assert settings == {"session_ttl_s": 900, "max_session_entries": 128}
    assert any(level == "warning" for level, _ in logs.messages)


def test_settings_for_non_dict_block_falls_back(logs):
    settings = hook_config.settings_for({"time_router": "nope"}, "time_router")
    assert settings == {"session_ttl_s": 900, "max_session_entries": 128}
    assert any("not a dict" in text for _, text in logs.messages)


def test_loaded_settings_delegates():
    loaded = hook_config.Loaded({}, {"time_router": {"session_ttl_s": 5}})
    assert loaded.settings("time_router")["session_ttl_s"] == 5


# ------------------------------------------------------------------------ load
def test_load_missing_path_is_empty():
    loaded = hook_config.load("/no/such/config.yaml")
    assert loaded.models == {}
    assert loaded.raw_settings == {}


def test_load_no_config_path_warns(tmp_path, logs, monkeypatch):
    monkeypatch.setattr(hook_config, "CONFIG_PATHS", [str(tmp_path / "missing.yaml")])
    loaded = hook_config.load()
    assert loaded.models == {}
    assert any(level == "warning" and "no config file" in text for level, text in logs.messages)


def test_load_reads_file(tmp_path, valid_config):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(valid_config))

    loaded = hook_config.load(str(path))
    assert loaded.models["deepseek/deepseek-v4-flash"]["route"] == "deepseek"
    assert loaded.settings("time_router")["session_ttl_s"] == 60


def test_load_invalid_yaml_does_not_raise(tmp_path, logs):
    path = tmp_path / "config.yaml"
    path.write_text("{ not: [valid")

    loaded = hook_config.load(str(path))
    assert loaded.models == {}
    assert any(level == "error" and "cannot read" in text for level, text in logs.messages)


def test_load_refreshes_each_call(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model_list:\n  - model_name: a\n")
    first = hook_config.load(str(path))
    path.write_text("model_list:\n  - model_name: b\n")
    second = hook_config.load(str(path))

    assert "a" in first.models
    assert "a" not in second.models
    assert "b" in second.models


def test_find_config_path_honors_order(tmp_path):
    missing = tmp_path / "missing.yaml"
    present = tmp_path / "present.yaml"
    present.write_text("model_list: []\n")
    assert hook_config.find_config_path([str(missing), str(present)]) == str(present)
    assert hook_config.find_config_path([str(missing)]) is None
