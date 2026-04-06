"""Configuration from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv


def _project_root() -> Path:
    """Project root directory (parent of package `upwork`)."""
    return Path(__file__).resolve().parent.parent


@dataclass
class Config:
    """App configuration. Load via Config.from_env()."""

    upwork_feed_url: str
    upwork_search_keyword: str
    impersonate_browser: str
    flaresolverr_url: str
    telegram_bot_token: str
    gemini_api_key: str
    telegram_chat_id: str = ""
    poll_interval_seconds: int = 120
    seen_store_path: str = ".seen_jobs.json"
    telegram_subscribers_store_path: str = ".telegram_subscribers.json"
    gemini_model: str = "gemini-2.0-flash"
    # --- Upwork fetch (GraphQL + FlareSolverr + .auth only; HTML scrape removed) ---
    upwork_fetch_mode: str = "auto"
    upwork_auth_dir: Path = field(default_factory=lambda: _project_root() / ".auth")
    upwork_auto_login: bool = False
    upwork_email: str = ""
    upwork_password: str = ""
    graphql_sort: str = "recency+desc"
    graphql_page_size: int = 50
    graphql_403_max_retries: int = 3
    flaresolverr_timeout_ms: int = 120_000
    upwork_login_form: bool = True

    def resolved_fetch_mode(self) -> Literal["graphql"]:
        """
        Always GraphQL (FlareSolverr warm-up + requests POST userJobSearch).
        `UPWORK_FETCH_MODE=html` and `auto` are both treated as graphql - HTML branch removed.
        """
        m = (self.upwork_fetch_mode or "auto").strip().lower()
        if m == "html":
            import logging

            logging.getLogger("upwork.config").warning(
                "UPWORK_FETCH_MODE=html is no longer supported - GraphQL only. Remove it or set graphql/auto."
            )
        return "graphql"

    @staticmethod
    def from_env() -> "Config":
        load_dotenv()

        feed_url = os.getenv("UPWORK_FEED_URL", "").strip()
        search_keyword = os.getenv("UPWORK_SEARCH_KEYWORD", "").strip()
        if not feed_url and not search_keyword:
            raise ValueError(
                "Set at least one: UPWORK_FEED_URL (RSS) or UPWORK_SEARCH_KEYWORD (scrape)."
            )

        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not telegram_token:
            raise ValueError("Missing required env vars: TELEGRAM_BOT_TOKEN")

        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        keys_file = _project_root() / "api_key_gemini.txt"
        has_gemini_file = False
        if keys_file.exists():
            for line in keys_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    has_gemini_file = True
                    break

        nr_model = os.getenv("NINEROUTER_MODEL", "").strip()
        or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        or_model = os.getenv("OPENROUTER_MODEL", "").strip()

        has_gemini = bool(gemini_key) or has_gemini_file
        has_ninerouter = bool(nr_model)
        has_openrouter = bool(or_key and or_model)
        if not (has_gemini or has_ninerouter or has_openrouter):
            raise ValueError(
                "At least one summary backend is required: GEMINI_API_KEY, "
                "or NINEROUTER_API_KEY + NINEROUTER_MODEL, "
                "or OPENROUTER_API_KEY + OPENROUTER_MODEL."
            )

        auth_dir_raw = os.getenv("UPWORK_AUTH_DIR", "").strip()
        auth_dir = Path(auth_dir_raw).expanduser().resolve() if auth_dir_raw else _project_root() / ".auth"

        auto_login = os.getenv("UPWORK_AUTO_LOGIN", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        return Config(
            upwork_feed_url=feed_url,
            upwork_search_keyword=search_keyword,
            impersonate_browser=os.getenv("UPWORK_IMPERSONATE", "chrome120").strip() or "chrome120",
            flaresolverr_url=os.getenv("FLARESOLVERR_URL", "").strip(),
            telegram_bot_token=telegram_token,
            gemini_api_key=gemini_key,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "120")),
            seen_store_path=os.getenv("SEEN_STORE_PATH", ".seen_jobs.json"),
            telegram_subscribers_store_path=os.getenv(
                "TELEGRAM_SUBSCRIBERS_STORE_PATH", ".telegram_subscribers.json"
            ),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            upwork_fetch_mode=os.getenv("UPWORK_FETCH_MODE", "auto").strip() or "auto",
            upwork_auth_dir=auth_dir,
            upwork_auto_login=auto_login,
            upwork_email=os.getenv("UPWORK_EMAIL", "").strip(),
            upwork_password=os.getenv("UPWORK_PASSWORD", "").strip(),
            graphql_sort=os.getenv("UPWORK_GRAPHQL_SORT", "recency+desc").strip() or "recency+desc",
            graphql_page_size=int(os.getenv("UPWORK_GRAPHQL_PAGE_SIZE", "50")),
            graphql_403_max_retries=int(os.getenv("UPWORK_GRAPHQL_403_MAX_RETRIES", "3")),
            flaresolverr_timeout_ms=int(os.getenv("FLARESOLVERR_TIMEOUT_MS", "120000")),
            upwork_login_form=os.getenv("UPWORK_LOGIN_FORM", "1").strip().lower()
            in ("1", "true", "yes", "on"),
        )
