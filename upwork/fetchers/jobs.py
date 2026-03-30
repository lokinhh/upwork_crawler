"""Gom keyword → GraphQL userJobSearch (FlareSolverr + .auth). Không còn fetch HTML."""
from __future__ import annotations

import logging
from typing import Dict, List

from ..config import Config
from .graphql_search import fetch_jobs_graphql
from .keyword import user_query_from_search_keyword

LOGGER = logging.getLogger("upwork.fetchers.jobs")


def fetch_jobs_for_keywords(config: Config) -> List[Dict[str, str]]:
    """
    Đọc `UPWORK_SEARCH_KEYWORD` (phân tách bằng dấu phẩy), gọi GraphQL,
    gộp kết quả và loại trùng `id`.
    """
    raw = (config.upwork_search_keyword or "").strip()
    if not raw:
        LOGGER.error("UPWORK_SEARCH_KEYWORD trống.")
        return []

    if not config.flaresolverr_url:
        LOGGER.error("FLARESOLVERR_URL trống — bắt buộc cho GraphQL + Cloudflare.")
        return []

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return []

    LOGGER.info("Fetch: GraphQL userJobSearch (UPWORK_FETCH_MODE=%s)", config.upwork_fetch_mode)

    all_jobs: List[Dict[str, str]] = []
    seen_ids: set[str] = set()

    for idx, kw in enumerate(parts):
        LOGGER.info("Fetching jobs for keyword[%s]=%s", idx, kw[:200])

        uq = user_query_from_search_keyword(kw)
        if not uq:
            LOGGER.warning("Bỏ qua keyword rỗng sau chuẩn hoá: %r", kw)
            continue
        jobs = fetch_jobs_graphql(
            uq,
            config.upwork_auth_dir,
            config.flaresolverr_url,
            config=config,
            sort=config.graphql_sort,
            offset=0,
            count=config.graphql_page_size,
            timeout_ms=config.flaresolverr_timeout_ms,
        )

        for job in jobs:
            jid = job.get("id")
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)
            all_jobs.append(job)

    return all_jobs
