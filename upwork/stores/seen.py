"""Persistent set of seen job IDs."""
import json
import logging
from pathlib import Path
from typing import Set

LOGGER = logging.getLogger("upwork.stores.seen")


class SeenStore:
    """Stores job IDs that have already been sent to avoid duplicates."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.seen: Set[str] = self._load()

    def _load(self) -> Set[str]:
        if not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return set(data if isinstance(data, list) else [])
        except Exception:
            LOGGER.warning("Seen store is invalid, reset: %s", self.path)
            return set()

    def has(self, item_id: str) -> bool:
        return item_id in self.seen

    def add(self, item_id: str) -> None:
        self.seen.add(item_id)

    def persist(self) -> None:
        payload = list(self.seen)[-5000:]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
