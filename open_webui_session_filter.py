"""
Open WebUI Filter Function — stamp the Open WebUI chat id as `metadata.session_id`.

Why: the LiteLLM `time_router` hook (sticky sessions) keys per-conversation state on
`data["metadata"]["session_id"]`. Open WebUI does not send any per-chat session id
natively, so this filter injects it into the request body before it reaches LiteLLM.
The pipe forwards the whole body (`payload = {**body, ...}`), so no pipe changes needed.

Setup:
  1. Admin > Functions > create a Function of type "filter" with this code.
  2. Enable it on the model(s) that use the alias `litellm/deepseek-v4-flash`
     (Model Settings > Filters). Prefer model-scoped over global.
  3. Send one message; if chat_id is not found, enable the debug print below.

Notes:
  - `request()` (Open WebUI >= 0.11.2) runs on EVERY outgoing model call, so the
    stamp survives tool loops. `inlet()` is the once-per-turn fallback for older
    versions. Define both; the runtime uses whichever its version supports.
  - The stamp is idempotent (setdefault semantics on the metadata dict).
"""
import os
from typing import Optional

from pydantic import BaseModel

# Debug: set this in the Function's environment or just flip to True while testing.
SESSION_DEBUG = os.environ.get("OWUI_SESSION_DEBUG") == "1"


class Filter:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ sources
    @staticmethod
    def _find_chat_id(body: dict, meta: Optional[dict]) -> Optional[str]:
        """chat_id from every plausible location, in priority order."""
        body_md = body.get("metadata") if isinstance(body, dict) else None
        if not isinstance(body_md, dict):
            body_md = {}
        if not isinstance(meta, dict):
            meta = {}

        return (
            body.get("chat_id")
            or body.get("session_id")
            or body.get("id")
            or body_md.get("chat_id")
            or body_md.get("session_id")
            or body_md.get("id")
            or meta.get("chat_id")
            or meta.get("session_id")
            or meta.get("id")
        )

    # ------------------------------------------------------------------ stamping
    def _stamp(self, body: dict, meta: Optional[dict]) -> dict:
        chat_id = self._find_chat_id(body, meta)
        if chat_id:
            body.setdefault("metadata", {})["session_id"] = str(chat_id)
        elif SESSION_DEBUG:
            print(
                "[session-filter] no chat_id found. "
                f"body keys={sorted(k for k in body if not k.startswith('__'))} "
                f"meta keys={sorted(meta) if isinstance(meta, dict) else meta}",
                flush=True,
            )
        return body

    # ------------------------------------------------------------ hook points
    async def request(self, body: dict, __metadata__: Optional[dict] = None) -> dict:
        """v0.11.2+: every outgoing model call (survives tool loops)."""
        return self._stamp(body, __metadata__)

    async def inlet(self, body: dict, __metadata__: Optional[dict] = None) -> dict:
        """Fallback for versions without `request` (once per user turn)."""
        return self._stamp(body, __metadata__)
