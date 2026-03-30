"""Helpers to parse Upwork search HTML into job dicts."""
import json
import logging
import re
from typing import Any, Dict, List

from bs4 import BeautifulSoup

LOGGER = logging.getLogger("upwork.fetchers.scrape")

# Job dict keys for downstream: id, title, link, description, published
UPWORK_SEARCH_URL = "https://www.upwork.com/nx/search/jobs"
MAX_JSON_DEPTH = 10
JOB_KEYS = ("results", "jobs", "jobPostings", "edges")


def save_html_to_file(html: str, path: str = "upwork_html.html") -> None:
    """Debug helper: dump raw HTML to a file for inspection."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as exc:
        LOGGER.warning("Failed to save HTML to %s: %s", path, exc)


def _extract_jobs_from_json(data: Any, depth: int = 0) -> List[Dict[str, str]]:
    """Recursively find job items (dict with title + ciphertext) in JSON-like data."""
    if depth > MAX_JSON_DEPTH:
        return []

    out: List[Dict[str, str]] = []

    if isinstance(data, dict):
        for key in JOB_KEYS:
            if key in data:
                out.extend(_extract_jobs_from_json(data[key], depth + 1))
        for v in data.values():
            if isinstance(v, (dict, list)):
                out.extend(_extract_jobs_from_json(v, depth + 1))

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "title" in item and "ciphertext" in item:
                cid = item.get("ciphertext", "").strip()
                if not cid:
                    continue
                desc = (item.get("description") or "")[:500]
                out.append(
                    {
                        "id": cid,
                        "title": (item.get("title") or "N/A").strip(),
                        "link": f"https://www.upwork.com/jobs/{cid}",
                        "description": desc,
                        "published": (item.get("publishedOn") or "").strip(),
                    }
                )
            elif isinstance(item, (dict, list)):
                out.extend(_extract_jobs_from_json(item, depth + 1))

    return out


def _parse_jobs_from_html(html: str) -> List[Dict[str, str]]:
    """
    Parse HTML returned by Upwork search (via FlareSolverr or curl_cffi).

    Strategy:
    1) Try to find <script type="application/json"> blocks with job data (older layout).
    2) Nếu không có, parse trực tiếp DOM JobTile (UI hiện tại).
    3) Cuối cùng, fallback regex scan trên raw HTML để bắt "ciphertext".
    """
    save_html_to_file(html)

    LOGGER.debug("Parsing Upwork HTML, length=%s", len(html))

    soup = BeautifulSoup(html, "html.parser")
    jobs: List[Dict[str, str]] = []

    # --- Path 1: JSON scripts (may still exist on some layouts) ---
    for script in soup.find_all("script", type="application/json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
            jobs.extend(_extract_jobs_from_json(data))
        except Exception:
            continue

    if jobs:
        return jobs

    # --- Path 2: DOM parsing of job tiles (current Upwork UI) ---
    LOGGER.debug("No jobs found via JSON scripts, trying DOM job tiles")
    seen_ids = set()

    job_tiles = soup.find_all(attrs={"data-test": "JobTile"})
    for tile in job_tiles:
        # Title + link
        link_el = tile.select_one('a[data-test*="job-tile-title-link"]') or tile.find(
            "a", attrs={"data-test": re.compile(r"\bjob-tile-title-link\b")}
        )
        if not link_el:
            continue

        href = link_el.get("href") or ""
        title = link_el.get_text(strip=True) or "N/A"

        # ID: ưu tiên ciphertext trong URL (~...), fallback sang data-ev-job-uid
        cid = None
        m = re.search(r"~([^/?]+)", href)
        if m:
            cid = m.group(1).strip()
        if not cid:
            cid = (link_el.get("data-ev-job-uid") or link_el.get("data-ev-opening_uid") or "").strip()
        if not cid or cid in seen_ids:
            continue

        # Published text (e.g. "Posted 1 hour ago")
        published_el = tile.select_one('[data-test="job-pubilshed-date"]')
        published = ""
        if published_el:
            published = published_el.get_text(" ", strip=True)

        # Job meta: type (hourly/fixed), experience level, budget string
        job_type_el = tile.select_one('[data-test="job-type-label"]')
        job_type = job_type_el.get_text(" ", strip=True) if job_type_el else ""

        exp_el = tile.select_one('[data-test="experience-level"]')
        experience_level = exp_el.get_text(" ", strip=True) if exp_el else ""

        budget = ""
        budget_li = tile.select_one('[data-test="is-fixed-price"]')
        if budget_li:
            # Typically: "Est. budget: $300.00"
            budget = budget_li.get_text(" ", strip=True)

        # Description (job summary under title)
        desc_el = tile.select_one('[data-test*="JobDescription"] p') or tile.select_one(
            '[data-test*="JobDescription"]'
        )
        if not desc_el:
            # Fallback: first paragraph inside tile
            desc_el = tile.find("p")
        desc = ""
        if desc_el:
            desc = desc_el.get_text(" ", strip=True)[:500]

        jobs.append(
            {
                "id": cid,
                "title": title,
                "link": f"https://www.upwork.com{href}" if href.startswith("/") else href,
                "description": desc,
                "published": published,
                "job_type": job_type,
                "experience_level": experience_level,
                "budget": budget,
            }
        )
        seen_ids.add(cid)

    if jobs:
        return jobs

    # --- Path 3: Regex fallback over raw HTML / JS state ---
    LOGGER.debug("No jobs found via DOM tiles, falling back to regex scan")
    seen_ids = set()
    # Find every occurrence of "ciphertext":"<id>" and build a small window
    # around it to pull title/description/publishedOn.
    for match in re.finditer(r'"ciphertext"\s*:\s*"(?P<cid>[^"]+)"', html):
        cid = match.group("cid").strip()
        if not cid or cid in seen_ids:
            continue

        window_start = max(0, match.start() - 800)
        window_end = min(len(html), match.end() + 800)
        chunk = html[window_start:window_end]

        title_match = re.search(r'"title"\s*:\s*"([^"]+)"', chunk)
        desc_match = re.search(r'"description"\s*:\s*"([^"]*)"', chunk)
        pub_match = re.search(r'"publishedOn"\s*:\s*"([^"]*)"', chunk)

        title = (title_match.group(1) if title_match else "N/A").strip()
        desc = (desc_match.group(1) if desc_match else "")[:500]
        published = (pub_match.group(1) if pub_match else "").strip()

        jobs.append(
            {
                "id": cid,
                "title": title,
                "link": f"https://www.upwork.com/jobs/{cid}",
                "description": desc,
                "published": published,
            }
        )
        seen_ids.add(cid)

    return jobs


# Thứ tự thử impersonate khi bị 403 (curl_cffi)
IMPERSONATE_FALLBACKS = ("chrome120", "safari17_0", "chrome119", "safari15_5", "edge101")


def fetch_jobs_from_scrape(
    keyword: str,
    impersonate: str = "chrome120",
    timeout: int = 30,
) -> List[Dict[str, str]]:
    """
    Legacy helper that used curl_cffi to fetch HTML and then parse with
    `_parse_jobs_from_html`. Kept for debugging/manual use; the main scanner
    path should prefer FlareSolverr or RSS and not rely on curl_cffi anymore.
    """
    from curl_cffi import requests as curl_requests

    params = {"q": keyword, "sort": "recency"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.upwork.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
    }

    resp = curl_requests.get(
        UPWORK_SEARCH_URL,
        params=params,
        headers=headers,
        impersonate=impersonate,
        timeout=timeout,
    )

    if resp.status_code != 200:
        LOGGER.error("Scrape via curl_cffi failed: status=%s", resp.status_code)
        return []

    jobs = _parse_jobs_from_html(resp.text)
    if not jobs:
        LOGGER.debug("Scrape 200 but 0 jobs (HTML/JSON co the doi)")
    return jobs
