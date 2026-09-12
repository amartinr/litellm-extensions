"""Pytest bootstrap.

Puts the repo root (``hook_config``) and the two hook directories on
``sys.path``. The hooks are imported as top-level modules (mounted flat at
``/app`` in production); their test files import them by module name.

LiteLLM is not a test dependency. The hook modules import
``litellm.integrations.custom_logger`` at module level, so a minimal stub is
installed before collection. ``hook_config`` only imports litellm lazily and
falls back to stdlib logging.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for _path in (ROOT, ROOT / "time_router", ROOT / "reasoning_route_adapter"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _install_litellm_stub() -> None:
    if "litellm" in sys.modules:
        return
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


_install_litellm_stub()
