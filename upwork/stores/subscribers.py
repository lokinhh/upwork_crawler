"""Telegram subscribers (chat IDs) and getUpdates offset."""
import json
import logging
from pathlib import Path
from typing import List, Set

LOGGER = logging.getLogger("upwork.stores.subscribers")


class TelegramSubscribersStore:
    """Stores Telegram chat IDs and last update_id for getUpdates."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.chat_ids: Set[str] = set()
        self.last_update_id: int = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw_chat_ids = data.get("chat_ids", [])
                self.chat_ids = set(str(item) for item in raw_chat_ids)
                self.last_update_id = int(data.get("last_update_id", 0))
        except Exception:
            LOGGER.warning("Subscribers store is invalid, reset: %s", self.path)

    def add_chat_id(self, chat_id: str) -> bool:
        before = len(self.chat_ids)
        self.chat_ids.add(str(chat_id))
        return len(self.chat_ids) > before

    def set_last_update_id(self, update_id: int) -> None:
        if update_id > self.last_update_id:
            self.last_update_id = update_id

    def get_chat_ids(self) -> List[str]:
        return sorted(self.chat_ids)

    def persist(self) -> None:
        payload = {
            "chat_ids": sorted(self.chat_ids),
            "last_update_id": self.last_update_id,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
