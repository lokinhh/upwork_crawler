"""Wrapper client: 9Router first, then OpenRouter, then Gemini."""
import logging
from typing import Dict, Optional

from .gemini import GeminiClient
from .ninerouter import NineRouterClient
from .openrouter import OpenRouterClient

LOGGER = logging.getLogger("upwork.clients.summarizer")

_FAILURE_PREFIX = "Khong the tom tat tu dong cho job nay"


def _looks_like_failure(text: str) -> bool:
    """Heuristic: Gemini/OpenRouter/9Router failure strings (with or without diacritics)."""
    t = (text or "").strip().lower()
    if t.startswith(_FAILURE_PREFIX.lower()):
        return True
    if "không thể tóm tắt" in t or "khong the tom tat" in t:
        return True
    return False


class SummarizerClient:
    """
    High-level summarizer used by the scanner.

    - Ưu tiên 9Router (OpenAI-compatible, thường chạy local).
    - Fallback OpenRouter, rồi Gemini (nếu được cấu hình).
    """

    def __init__(
        self,
        gemini: Optional[GeminiClient] = None,
        openrouter: Optional[OpenRouterClient] = None,
        ninerouter: Optional[NineRouterClient] = None,
    ) -> None:
        if not gemini and not openrouter and not ninerouter:
            raise ValueError(
                "SummarizerClient requires at least one backend (9Router/OpenRouter/Gemini)"
            )
        self.gemini = gemini
        self.openrouter = openrouter
        self.ninerouter = ninerouter

    def summarize(self, job: Dict[str, str]) -> str:
        # 1) 9Router
        if self.ninerouter:
            try:
                text = self.ninerouter.summarize(job)
                if not _looks_like_failure(text):
                    return text
                LOGGER.warning("9Router summary looks like a failure, trying fallback...")
            except Exception as exc:
                LOGGER.exception("9Router summarize raised exception, trying fallback: %s", exc)

        # 2) OpenRouter
        if self.openrouter:
            try:
                text = self.openrouter.summarize(job)
                if not _looks_like_failure(text):
                    return text
                LOGGER.warning(
                    "OpenRouter summary looks like a failure, trying Gemini fallback..."
                )
            except Exception as exc:
                LOGGER.exception("OpenRouter summarize raised exception, trying fallback: %s", exc)

        # 3) Gemini
        if self.gemini:
            try:
                return self.gemini.summarize(job)
            except Exception as exc:
                LOGGER.exception("Gemini summarize failed: %s", exc)

        return f"{_FAILURE_PREFIX} (tat ca backend deu that bai)."

