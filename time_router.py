import os
from datetime import datetime, timezone
import yaml
from litellm.integrations.custom_logger import CustomLogger

CONFIG_PATHS = [
    os.environ.get("LITELLM_CONFIG_FILE", ""),   # env var wins if set
    "/app/config.yaml",                          # real path
    "./config.yaml",
]

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
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Only intercept the public alias used by clients
        if data.get("model") == "litellm/deepseek-v4-flash":
            h = datetime.now(timezone.utc).hour
            # DeepSeek peak windows: 01:00-04:00 and 06:00-10:00 UTC
            is_peak = (1 <= h < 4) or (6 <= h < 10)
            target = "openrouter/deepseek-v4-flash" if is_peak else "deepseek/deepseek-v4-flash"

            data["model"] = target

            # metadata.route of the TARGET model (from config.yaml)
            # -> custom_prometheus_metadata_labels exposes it as metadata_route
            route = ROUTE_MAP.get(target, "unknown")

            metadata = data.setdefault("metadata", {})

            # (a) request-level: visible to other consumers / debugging
            metadata["route"] = route

            # (b) PROMETHEUS FIX: custom labels are NOT read from top-level
            # request metadata. LiteLLM builds the standard logging payload from a
            # whitelist (StandardLoggingMetadata annotations) and copies
            # `requester_metadata` BY REFERENCE — a deepcopy snapshot taken in
            # add_litellm_data_to_request BEFORE pre-call hooks run. Writing the
            # key into that same nested dict is the only pre-call injection that
            # reaches `_get_combined_custom_metadata_from_standard_logging_payload`
            # and therefore the `metadata_route` label.
            metadata.setdefault("requester_metadata", {})["route"] = route

            # (c) same value under spend_logs_metadata covers consumers that read
            # that bucket instead (spend logs / dashboards). Harmless redundancy.
            metadata.setdefault("spend_logs_metadata", {})["route"] = route

            if os.environ.get("TIME_ROUTER_DEBUG"):
                import json as _json
                print(f"[TimeRouter] model={data.get('model')!r} target={target} route={route}", flush=True)
                print(f"[TimeRouter] ROUTE_MAP={_json.dumps(ROUTE_MAP)}", flush=True)
                print(f"[TimeRouter] metadata after inject={_json.dumps(metadata, default=str)}", flush=True)
        return data

proxy_handler_instance = TimeRouter()
