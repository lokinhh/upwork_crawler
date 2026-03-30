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
        f"[Upwork Job Mới]\n"
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
        """Fetch jobs qua GraphQL userJobSearch (FlareSolverr + .auth), không fetch HTML.

        Hỗ trợ nhiều keyword/URL trong UPWORK_SEARCH_KEYWORD nếu phân tách bằng dấu phẩy.
        Ví dụ:
        - "spring boot"
        - "spring boot,java backend"
        - "https://www.upwork.com/nx/search/jobs?... , https://www.upwork.com/nx/search/jobs?..."
        """
        if not self.config.upwork_search_keyword:
            LOGGER.error("UPWORK_SEARCH_KEYWORD chưa được cấu hình; không thể fetch job.")
            return []

        if not self.config.flaresolverr_url:
            LOGGER.error(
                "FLARESOLVERR_URL trống. Bật FlareSolverr (Docker) và đặt FLARESOLVERR_URL "
                "hoặc sửa cấu hình nếu muốn dùng nguồn khác."
            )
            return []

        try:
            ensure_graphql_session(self.config)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            LOGGER.error("Không thể chuẩn bị phiên GraphQL: %s", exc)
            return []
        except subprocess.CalledProcessError as exc:
            LOGGER.error("Đăng nhập tự động thất bại (exit %s).", exc.returncode)
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
                    # /start gui trong luc fetch job khong duoc lan sync dau vong; sync lai truoc khi gui.
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
