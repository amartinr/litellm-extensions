"""
title: LiteLLM Session Stamp
id: litellm_session_stamp
author: A. Martin
author_url: https://github.com/amartinr
git_url: https://github.com/amartinr/open-webui-extensions.git
description: >
    Stamps the Open WebUI chat id as `metadata.session_id` on every request
    sent to LiteLLM, so the LiteLLM time_router hook can key per-conversation
    state (sticky sessions). Open WebUI does not send a per-chat session id
    natively; this filter injects it into the request body, which the LiteLLM
    pipe forwards unchanged (`{**body, ...}`). Chat id is read from
    `__chat_id__` first, then `__metadata__`, then the body. Idempotent and
    safe to run on every outgoing call. Attach to the model(s) that use the
    `litellm/deepseek-v4-flash` alias (Model Settings > Filters); keep it
    model-scoped, not global.
required_open_webui_version: 0.9.0
version: 1.0.0
licence: MIT
"""

import logging
from typing import Optional

from pydantic import BaseModel

log = logging.getLogger(__name__)


class Filter:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ sources
    @staticmethod
    def _find_chat_id(
        body: dict,
        meta: Optional[dict],
        chat_id: Optional[str],
    ) -> Optional[str]:
        """Open WebUI chat id from every plausible location, in priority order."""
        if chat_id:
            return chat_id
        if isinstance(meta, dict):
            for key in ("chat_id", "session_id", "id"):
                value = meta.get(key)
                if value:
                    return value
        if isinstance(body, dict):
            body_meta = body.get("metadata")
            if isinstance(body_meta, dict):
                for key in ("chat_id", "session_id", "id"):
                    value = body_meta.get(key)
                    if value:
                        return value
            for key in ("chat_id", "session_id", "id"):
                value = body.get(key)
                if value:
                    return value
        return None

    # ------------------------------------------------------------------ stamping
    def _stamp(
        self,
        body: dict,
        meta: Optional[dict],
        chat_id: Optional[str],
    ) -> dict:
        """Write metadata.session_id once the chat id is known. Idempotent."""
        sid = self._find_chat_id(body, meta, chat_id)
        if sid:
            body.setdefault("metadata", {})["session_id"] = str(sid)
        else:
            # Rare: title generation and other out-of-chat calls have no chat id.
            # LiteLLM falls back to stateless hour-based routing for these.
            log.info("litellm_session_stamp: no chat/session id found on request")
        return body

    # ------------------------------------------------------------ hook points
    async def request(
        self,
        body: dict,
        __chat_id__: Optional[str] = None,
        __metadata__: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        """Open WebUI >= 0.11.2: runs on every outgoing model call (survives tool loops)."""
        return self._stamp(body, __metadata__, __chat_id__)

    async def inlet(
        self,
        body: dict,
        __chat_id__: Optional[str] = None,
        __metadata__: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        """Fallback for versions without `request` (once per user turn)."""
        return self._stamp(body, __metadata__, __chat_id__)
