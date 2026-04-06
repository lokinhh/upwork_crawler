"""Orchestrator: fetch jobs -> filter new -> summarize -> send to Telegram."""
import logging
import subprocess
import time
from typing import Dict, List

from .config import Config
from .fetchers.jobs import fetch_jobs_for_keywords
from .session.ensure import ensure_graphql_session
from .stores import SeenStore, TelegramSubscribersStore
from .clients import SummarizerClient, TelegramClient

LOGGER = logging.getLogger("upwork.scanner")


def build_telegram_message(job: Dict[str, str], summary: str) -> str:
    """Build the text to send for one job (with Gemini summary)."""
    title = job.get("title", "Untitled")
    link = job.get("link", "")
    published = job.get("published", "N/A")
    return (
        f"[New Upwork Job]\n"
        f"Title: {title}\n"
        f"Published: {published}\n"
        f"Link: {link}\n\n"
        f"{summary}"
    )[:3900]


class UpworkScanner:
    """Main loop: sync subscribers, fetch jobs, dedupe, summarize, send."""

    def __init__(
        self,
        config: Config,
        seen_store: SeenStore,
        subscribers_store: TelegramSubscribersStore,
        summarizer: SummarizerClient,
        telegram: TelegramClient,
    ) -> None:
        self.config = config
        self.seen_store = seen_store
        self.subscribers_store = subscribers_store
        self.summarizer = summarizer
        self.telegram = telegram

    def _fetch_jobs(self) -> List[Dict[str, str]]:
        """Fetch jobs via GraphQL userJobSearch (FlareSolverr + .auth), no HTML fetch.

        Supports multiple keywords/URLs in UPWORK_SEARCH_KEYWORD when comma-separated.
        Example:
        - "spring boot"
        - "spring boot,java backend"
        - "https://www.upwork.com/nx/search/jobs?... , https://www.upwork.com/nx/search/jobs?..."
        """
        if not self.config.upwork_search_keyword:
            LOGGER.error("UPWORK_SEARCH_KEYWORD is not configured; cannot fetch jobs.")
            return []

        if not self.config.flaresolverr_url:
            LOGGER.error(
                "FLARESOLVERR_URL is empty. Start FlareSolverr (Docker) and set FLARESOLVERR_URL "
                "or update config if you want to use another source."
            )
            return []

        try:
            ensure_graphql_session(self.config)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            LOGGER.error("Cannot prepare GraphQL session: %s", exc)
            return []
        except subprocess.CalledProcessError as exc:
            LOGGER.error("Automatic login failed (exit %s).", exc.returncode)
            return []

        return fetch_jobs_for_keywords(self.config)

    def run_forever(self) -> None:
        LOGGER.info("Starting scanner...")
        LOGGER.info("Poll interval: %ss", self.config.poll_interval_seconds)

        while True:
            try:
                recipients = self.telegram.sync_subscribers(self.subscribers_store)
                jobs = self._fetch_jobs()
                new_jobs = [j for j in jobs if not self.seen_store.has(j["id"])]

                if new_jobs:
                    # If /start arrives during fetch, first sync may miss it; sync again before sending.
                    recipients = self.telegram.sync_subscribers(self.subscribers_store)
                    LOGGER.info("Found %s new jobs", len(new_jobs))
                else:
                    LOGGER.info("No new jobs")

                for job in reversed(new_jobs):
                    if not recipients:
                        LOGGER.warning(
                            "No Telegram subscribers yet. Send /start to bot first."
                        )
                        break
                    summary = self.summarizer.summarize(job)
                    message = build_telegram_message(job, summary)
                    sent_count = 0
                    for chat_id in recipients:
                        try:
                            self.telegram.send_message(message, chat_id=chat_id)
                            sent_count += 1
                        except Exception:
                            LOGGER.exception("Failed sending to chat_id=%s", chat_id)
                    if sent_count == 0:
                        LOGGER.warning("Skip mark-as-seen because send failed")
                        continue
                    self.seen_store.add(job["id"])
                    time.sleep(1.0)

                if new_jobs:
                    self.seen_store.persist()

            except Exception as exc:
                LOGGER.exception("Scan cycle failed: %s", exc)

            time.sleep(self.config.poll_interval_seconds)
