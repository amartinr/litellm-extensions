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
            data.setdefault("metadata", {})["route"] = route
        return data

proxy_handler_instance = TimeRouter()
