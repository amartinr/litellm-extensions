import os
from datetime import datetime, timezone
import yaml
from litellm.integrations.custom_logger import CustomLogger

CONFIG_PATHS = [
    os.environ.get("LITELLM_CONFIG_FILE", ""),   # env var wins if set
    "/app/config.yaml",                          # real path
    "./config.yaml",
]

ALIAS_MODEL = "litellm/deepseek-v4-flash"
PEAK_TARGET = "openrouter/deepseek-v4-flash"
OFFPEAK_TARGET = "deepseek/deepseek-v4-flash"

def _load_route_map():
    """Builds {model_name: route} from model_info.metadata.route in config.yaml."""
    for p in CONFIG_PATHS:
        if p and os.path.exists(p):
            with open(p) as f:
                cfg = yaml.safe_load(f)
            route_map = {}
            for m in (cfg.get("model_list") or []):
                name = m.get("model_name")
                route = (m.get("model_info") or {}).get("metadata", {}).get("route")
                if name and route:
                    route_map[name] = route
            return route_map
    return {}

ROUTE_MAP = _load_route_map()

class TimeRouter(CustomLogger):
    @staticmethod
    def _inject_route(data: dict, route: str) -> dict:
        """Write route where LiteLLM's Prometheus labels can read it.

        Custom labels come from the standard logging payload, which only keeps
        whitelisted metadata keys (StandardLoggingMetadata annotations) and copies
        `requester_metadata` by reference — a deepcopy snapshot taken by
        add_litellm_data_to_request BEFORE pre-call hooks run. Top-level metadata
        keys are dropped, so the value must go into requester_metadata (and
        spend_logs_metadata, read by spend-log consumers).
        """
        metadata = data.setdefault("metadata", {})
        metadata["route"] = route                                    # request-level, debug/other consumers
        metadata.setdefault("requester_metadata", {})["route"] = route   # -> metadata_route label
        metadata.setdefault("spend_logs_metadata", {})["route"] = route  # -> spend logs
        return metadata

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        requested = data.get("model")
        route = None
        target = None

        if requested == ALIAS_MODEL:
            # Time-based routing for the public alias.
            h = datetime.now(timezone.utc).hour
            # DeepSeek peak windows: 01:00-04:00 and 06:00-10:00 UTC
            is_peak = (1 <= h < 4) or (6 <= h < 10)
            target = PEAK_TARGET if is_peak else OFFPEAK_TARGET
            data["model"] = target
            route = ROUTE_MAP.get(target, "unknown")
        elif requested in ROUTE_MAP:
            # Direct call to a model that declares a route in config.yaml:
            # label it with the declared route, no rerouting.
            route = ROUTE_MAP[requested]

        if route is not None:
            metadata = self._inject_route(data, route)
            if os.environ.get("TIME_ROUTER_DEBUG"):
                import json as _json
                print(
                    f"[TimeRouter] requested={requested!r} target={target!r} route={route}",
                    flush=True,
                )
                print(f"[TimeRouter] ROUTE_MAP={_json.dumps(ROUTE_MAP)}", flush=True)
                print(
                    f"[TimeRouter] metadata after inject={_json.dumps(metadata, default=str)}",
                    flush=True,
                )
        return data

proxy_handler_instance = TimeRouter()
