"""
GraphQL userJobSearch + FlareSolverr — logic đồng bộ với debug_upwork_graphql/graphql_via_flaresolverr.py.

FlareSolverr: giải Cloudflare → merge cookie.
requests: POST GraphQL.

Scanner: chèn `userQuery` / paging / sort vào body (file JSON giống debug).
403 (hoặc Challenge HTML): gọi lại login Playwright, tối đa `UPWORK_GRAPHQL_403_MAX_RETRIES` (mặc định 3).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import requests

from ..auth.loader import (
    describe_authorization_source,
    load_merged_auth,
    parse_cookie_header,
    resolve_authorization_header,
)

if TYPE_CHECKING:
    from ..config import Config

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

LOGGER = logging.getLogger("upwork.fetchers.graphql_search")

GRAPHQL = "https://www.upwork.com/api/graphql/v1?alias=userJobSearch"

# Giống graphql_via_flaresolverr.py
_GRAPHQL_REFERER_FROM_CAPTURE = (
    "https://www.upwork.com/nx/search/jobs/"
    "?from_recent_search=true&q=spring%20boot&page=2"
)

_DEFAULT_FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def _package_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


# Giống BODY_PATH / BODY_MINIMAL_PATH trong debug (tên file trong data/)
BODY_PATH = _package_data_dir() / "userJobSearch_body.json"
BODY_MINIMAL_PATH = _package_data_dir() / "postman_userJobSearch_body.minimal.json"


def _resolve_body_path() -> Path:
    custom = os.environ.get("UPWORK_GRAPHQL_BODY", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    if os.environ.get("UPWORK_GRAPHQL_MINIMAL", "").strip().lower() in ("1", "true", "yes", "on"):
        return BODY_MINIMAL_PATH
    return BODY_PATH


def _cookie_header_from_dict(d: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in d.items())


def _merge_cookies(existing_header: str, flaresolver_list: List[Dict[str, Any]]) -> str:
    d = parse_cookie_header(existing_header)
    for c in flaresolver_list:
        name = c.get("name")
        if not name:
            continue
        val = c.get("value")
        if val is None:
            continue
        d[str(name)] = str(val)
    return _cookie_header_from_dict(d)


def _browser_client_hints_headers() -> Dict[str, str]:
    return {
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Brave";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-arch": '"arm"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform-version": '"15.3.0"',
        "sec-ch-ua-full-version-list": (
            '"Chromium";v="146.0.0.0", "Not-A.Brand";v="24.0.0.0", "Brave";v="146.0.0.0"'
        ),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-gpc": "1",
        "Priority": "u=1, i",
    }


def _upwork_edge_headers(cookie_header: str) -> Dict[str, str]:
    ck = parse_cookie_header(cookie_header)
    visitor = ck.get("visitor_id", "").strip()
    span = str(uuid.uuid4())
    parent = str(uuid.uuid4())
    trace = f"{secrets.token_hex(8)}-PDX"
    h: Dict[str, str] = {
        "vnd-eo-span-id": span,
        "vnd-eo-parent-span-id": parent,
        "vnd-eo-trace-id": trace,
    }
    if visitor:
        h["vnd-eo-visitorid"] = visitor
    return h


def build_graphql_body_from_template(
    user_query: str,
    *,
    sort: str,
    offset: int,
    count: int,
    template_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Đọc JSON template (như debug), ghi đè requestVariables cho scanner."""
    path = template_path or _resolve_body_path()
    raw = path.read_text(encoding="utf-8")
    body: Dict[str, Any] = json.loads(raw)
    rv = body.setdefault("variables", {}).setdefault("requestVariables", {})
    rv["userQuery"] = user_query
    rv["sort"] = sort
    rv["paging"] = {"offset": max(0, int(offset)), "count": max(1, int(count))}
    if "highlight" not in rv:
        rv["highlight"] = True
    return body


def parse_jobs_from_graphql_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    try:
        data = payload.get("data") or {}
        search = data.get("search") or {}
        usn = search.get("universalSearchNuxt") or {}
        uj = usn.get("userJobSearchV1") or {}
        results = uj.get("results") or []
    except (TypeError, AttributeError):
        return []

    for row in results:
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip() or "N/A"
        job_tile = row.get("jobTile") or {}
        job = (job_tile.get("job") if isinstance(job_tile, dict) else None) or {}
        cid = (
            (job.get("cipherText") or job.get("ciphertext") or "").strip()
            or str(row.get("id") or "").strip()
        )
        if not cid:
            continue
        desc = (row.get("description") or "")[:500]
        published = (job.get("publishTime") or job.get("createTime") or "").strip()
        link = f"https://www.upwork.com/jobs/{cid}"
        out.append(
            {
                "id": cid,
                "title": title,
                "link": link,
                "description": desc,
                "published": published,
            }
        )
    return out


@dataclass
class _GraphqlAttemptResult:
    http_status: int
    content_type: str
    payload: Optional[Dict[str, Any]]
    graphql_errors: bool
    is_challenge_html: bool
    auth_src: str
    error_summary: str = ""


def _graphql_debug() -> bool:
    return os.environ.get("UPWORK_GRAPHQL_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _format_graphql_errors(payload: Optional[Dict[str, Any]], max_len: int = 800) -> str:
    if not isinstance(payload, dict):
        return ""
    errs = payload.get("errors")
    if not errs:
        return ""
    try:
        s = json.dumps(errs, ensure_ascii=False)
    except TypeError:
        s = repr(errs)
    return s[:max_len] + ("…" if len(s) > max_len else "")


def _execute_graphql_once(
    auth_dir: Path,
    flaresolverr_url: str,
    body_json: str,
    timeout_ms: int,
) -> _GraphqlAttemptResult:
    """Một vòng: warm FlareSolverr → merge → POST GraphQL (y hệt graphql_via_flaresolverr.main)."""
    auth = load_merged_auth(auth_dir)
    fs = (
        os.environ.get("FLARESOLVERR_URL", "").strip()
        or flaresolverr_url
        or auth["flaresolverr_url"]
    ).rstrip("/")
    warm_url = auth["warm_url"]
    cookie_user = auth["cookie"]
    tenant = auth["tenant_id"]

    api = f"{fs}/v1"
    LOGGER.debug("FlareSolverr GET warm_url=%s", warm_url)
    try:
        r = requests.post(
            api,
            json={"cmd": "request.get", "url": warm_url, "maxTimeout": timeout_ms},
            timeout=(10, timeout_ms // 1000 + 30),
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        LOGGER.warning("FlareSolverr warm failed: %s", e)
        return _GraphqlAttemptResult(0, "", None, False, False, "")

    if data.get("status") != "ok":
        LOGGER.warning("FlareSolverr status!=ok: %s", data.get("message", data))
        return _GraphqlAttemptResult(0, "", None, False, False, "")

    sol = data.get("solution") or {}
    fs_cookies = sol.get("cookies") or []
    fs_ua = (sol.get("userAgent") or "").strip()
    if os.environ.get("UPWORK_UA", "").strip():
        ua = os.environ["UPWORK_UA"].strip()
    elif os.environ.get("UPWORK_USE_FLARE_UA", "1").strip().lower() in ("0", "false", "no", "off"):
        ua = _DEFAULT_FALLBACK_UA
    else:
        ua = fs_ua or _DEFAULT_FALLBACK_UA

    merged = _merge_cookies(cookie_user, fs_cookies)
    merged_cookies = parse_cookie_header(merged)
    authorization = resolve_authorization_header(merged_cookies, auth_dir)
    src = describe_authorization_source(merged_cookies, auth_dir)
    LOGGER.info("GraphQL Authorization ← %s", src)

    referer = os.environ.get("UPWORK_GRAPHQL_REFERER", "").strip() or _GRAPHQL_REFERER_FROM_CAPTURE
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": os.environ.get("UPWORK_ACCEPT_LANGUAGE", "en-US").strip() or "en-US",
        "Origin": "https://www.upwork.com",
        "Referer": referer,
        "Authorization": authorization,
        "Cookie": merged,
        "x-upwork-api-tenantid": tenant,
        "x-upwork-accept-language": "en-US",
        "User-Agent": ua,
    }
    if os.environ.get("UPWORK_BROWSER_HINTS", "").strip().lower() in ("1", "true", "yes", "on"):
        headers.update(_browser_client_hints_headers())
    headers.update(_upwork_edge_headers(merged))
    if os.environ.get("UPWORK_GRAPHQL_MINIMAL_HEADERS", "").strip() in ("1", "true", "yes"):
        for k in list(_browser_client_hints_headers().keys()):
            headers.pop(k, None)

    LOGGER.debug("POST GraphQL (UA matches FlareSolverr: %s)", bool(fs_ua and ua == fs_ua))
    try:
        gr = requests.post(
            GRAPHQL,
            headers=headers,
            data=body_json.encode("utf-8"),
            timeout=120,
        )
    except requests.RequestException as e:
        LOGGER.warning("GraphQL POST failed: %s", e)
        return _GraphqlAttemptResult(0, "", None, False, False, src)

    ct = (gr.headers.get("content-type", "") or "").lower()
    raw_text = gr.text or ""
    body_preview = raw_text[:800]
    challenge = (
        gr.status_code == 403
        and "Challenge" in body_preview
        and "text/html" in ct
    )
    if challenge:
        LOGGER.warning(
            "Cloudflare/Upwork Challenge HTML (403) — thử login lại để làm mới phiên (nếu bật retry)."
        )

    payload: Optional[Dict[str, Any]] = None
    graphql_errors = False
    err_summary = ""

    def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
        t = (text or "").strip()
        if not t or not t.startswith("{"):
            return None
        try:
            out = json.loads(t)
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            return None

    # application/json hoặc body JSON (một số proxy đổi content-type)
    if ct.startswith("application/json") or "json" in ct:
        try:
            out = gr.json()
            if isinstance(out, dict):
                payload = out
        except (json.JSONDecodeError, ValueError):
            payload = _try_parse_json(raw_text)
    else:
        payload = _try_parse_json(raw_text)

    if isinstance(payload, dict):
        errs = payload.get("errors")
        if isinstance(errs, list) and len(errs) > 0:
            graphql_errors = True
            err0 = errs[0] if errs else {}
            msg = str((err0 or {}).get("message") or "")
            err_summary = _format_graphql_errors(payload)
            LOGGER.warning("GraphQL errors (HTTP %s): %s", gr.status_code, msg or err_summary[:400])
            if "oAuth2 client does not have permission" in msg:
                LOGGER.warning(
                    "Gợi ý: đặt bearer_cookie / bearer.txt khớp cookie OAuth — nguồn Bearer: %s",
                    src,
                )
        if _graphql_debug():
            LOGGER.info(
                "GraphQL DEBUG: status=%s ct=%r keys=%s errors=%s",
                gr.status_code,
                gr.headers.get("content-type"),
                list(payload.keys()),
                bool(payload.get("errors")),
            )
            LOGGER.info("GraphQL DEBUG body (truncated): %s", raw_text[:4000])
    else:
        LOGGER.warning(
            "GraphQL không parse được JSON (HTTP %s, content-type=%r). body[:400]=%r",
            gr.status_code,
            gr.headers.get("content-type"),
            raw_text[:400],
        )

    return _GraphqlAttemptResult(
        gr.status_code,
        gr.headers.get("content-type", "") or "",
        payload,
        graphql_errors,
        challenge,
        src,
        err_summary,
    )


def _should_relogin_after_result(res: _GraphqlAttemptResult) -> bool:
    if res.http_status == 403:
        return True
    if res.is_challenge_html:
        return True
    if res.payload and isinstance(res.payload, dict) and res.payload.get("errors"):
        err0 = (res.payload.get("errors") or [{}])[0]
        msg = str((err0 or {}).get("message") or "").lower()
        if any(
            x in msg
            for x in (
                "oauth2",
                "permission",
                "unauthorized",
                "expired",
                "invalid token",
                "authentication",
            )
        ):
            return True
    return False


def fetch_jobs_graphql(
    user_query: str,
    auth_dir: Path,
    flaresolverr_url: str,
    *,
    config: Optional["Config"] = None,
    sort: str = "recency+desc",
    offset: int = 0,
    count: int = 50,
    timeout_ms: int = 120_000,
) -> List[Dict[str, str]]:
    """
    Gọi GraphQL với retry: 403 / Challenge / lỗi OAuth → login Playwright + lưu `.auth` (tối đa N lần).
    Cần `config` có email/password để login lại.
    """
    body_path = _resolve_body_path()
    if not body_path.is_file():
        LOGGER.error("Thiếu file body GraphQL: %s", body_path)
        return []

    body = build_graphql_body_from_template(
        user_query,
        sort=sort,
        offset=offset,
        count=count,
        template_path=body_path,
    )
    body_json = json.dumps(body, ensure_ascii=False)

    max_retries = int(os.environ.get("UPWORK_GRAPHQL_403_MAX_RETRIES", "3"))
    if config is not None:
        max_retries = config.graphql_403_max_retries
    n = max(1, max_retries)

    from ..session.ensure import run_login_subprocess

    for attempt in range(n):
        res = _execute_graphql_once(auth_dir, flaresolverr_url, body_json, timeout_ms)

        if res.http_status and res.http_status < 400 and res.payload is not None:
            jobs = parse_jobs_from_graphql_payload(res.payload)
            if jobs:
                if res.graphql_errors:
                    LOGGER.warning(
                        "GraphQL có errors nhưng vẫn parse được %s job (partial success).",
                        len(jobs),
                    )
                return jobs
            if not res.graphql_errors:
                return []
            if attempt < n - 1 and config and config.upwork_email and config.upwork_password:
                if _should_relogin_after_result(res):
                    LOGGER.warning(
                        "GraphQL errors, không có job — login lại lần %s/%s",
                        attempt + 1,
                        n,
                    )
                    try:
                        run_login_subprocess(config)
                    except Exception:
                        LOGGER.exception("Login lại thất bại")
                        return []
                    continue
            LOGGER.warning(
                "GraphQL không có job (HTTP=%s). errors=%s",
                res.http_status,
                (res.error_summary or "")[:600] or "xem log GraphQL errors phía trên",
            )
            return []

        if attempt < n - 1 and config and config.upwork_email and config.upwork_password:
            if _should_relogin_after_result(res):
                LOGGER.warning(
                    "HTTP %s / Challenge — login lại lần %s/%s",
                    res.http_status or "?",
                    attempt + 1,
                    n,
                )
                try:
                    run_login_subprocess(config)
                except Exception:
                    LOGGER.exception("Login lại thất bại")
                    return []
                continue

        LOGGER.warning(
            "GraphQL thất bại (HTTP=%s, challenge=%s, có_payload=%s). %s",
            res.http_status,
            res.is_challenge_html,
            res.payload is not None,
            (res.error_summary or "—")[:500],
        )
        return []

    return []
