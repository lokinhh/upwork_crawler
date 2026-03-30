"""Telegram Bot API: send messages and sync /start subscribers."""
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ConnectTimeout, ReadTimeout, Timeout

from ..stores.subscribers import TelegramSubscribersStore

LOGGER = logging.getLogger("upwork.clients.telegram")


class TelegramClient:
    """Send messages to Telegram and maintain subscriber list from /start."""

    def __init__(self, bot_token: str, default_chat_id: str = "") -> None:
        self.bot_token = bot_token
        # * hoac all = gui cho moi chat da /start (khong gan chat co dinh)
        raw = (default_chat_id or "").strip()
        if raw == "*" or raw.casefold() == "all":
            self.default_chat_id = ""
        else:
            self.default_chat_id = raw
        self._http_timeout = float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "45").strip() or "45")
        self._http_retries = max(1, int(os.getenv("TELEGRAM_HTTP_RETRIES", "3").strip() or "3"))

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        """POST/GET to Telegram; retry on connect/read timeout (mang Docker / proxy cham)."""
        last_exc: Optional[BaseException] = None
        transient = (ConnectTimeout, ReadTimeout, Timeout, RequestsConnectionError)
        for attempt in range(self._http_retries):
            try:
                if method.upper() == "GET":
                    return requests.get(
                        url, params=params, timeout=self._http_timeout
                    )
                return requests.post(
                    url, json=json_body, timeout=self._http_timeout
                )
            except transient as exc:
                last_exc = exc
                if attempt + 1 >= self._http_retries:
                    break
                delay = min(8.0, 2.0**attempt)
                LOGGER.warning(
                    "Telegram HTTP %s failed (%s), retry %s/%s in %.1fs: %s",
                    method,
                    type(exc).__name__,
                    attempt + 1,
                    self._http_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def send_message(self, text: str, chat_id: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        response = self._request("POST", url, json_body=payload)
        response.raise_for_status()

    def sync_subscribers(self, store: TelegramSubscribersStore) -> List[str]:
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        offset = store.last_update_id + 1
        payload = {"offset": offset, "timeout": 0, "allowed_updates": ["message"]}
        response = self._request("GET", url, params=payload)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok", False):
            raise RuntimeError(f"Telegram getUpdates failed: {data}")

        added = 0
        updates = data.get("result", [])
        for update in updates:
            update_id = int(update.get("update_id", 0))
            store.set_last_update_id(update_id)

            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            if not text.startswith("/start"):
                continue

            chat = message.get("chat") or {}
            chat_id = str(chat.get("id", "")).strip()
            if not chat_id:
                continue
            if store.add_chat_id(chat_id):
                added += 1

        changed = False
        if self.default_chat_id:
            changed = store.add_chat_id(self.default_chat_id)

        if updates or added or changed:
            store.persist()

        if added:
            LOGGER.info("Added %s new Telegram subscribers", added)

        return store.get_chat_ids()
