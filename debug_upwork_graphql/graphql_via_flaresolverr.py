#!/usr/bin/env python3
"""
Gọi GraphQL userJobSearch sau khi làm mới cookie Cloudflare qua FlareSolverr.

Body mặc định — postman_userJobSearch_body.json — đồng bộ query + variables với request thành công trong
captures/debug_session_20260326_071440.log (userJobSearch REQUEST, dòng request body). Không dùng bản «minimal»
trừ khi bạn chủ động bật UPWORK_GRAPHQL_MINIMAL=1.

Tại sao cùng query mà web (Playwright/DevTools) chạy được, script gọi API lại hay lỗi OAuth / 403?

  1) Bearer phải là đúng OAuth client cho universal search. Trong log, DEBUG_TOKEN_MAP ghi Bearer khớp cookie
     «25897f9esb» — không phải lúc nào cũng cookie *fsb. Nếu script chọn nhầm cookie *fsb khác trong storage,
     Upwork trả «Requested oAuth2 client does not have permission…» dù query giống hệt. Cách chắc chắn: copy
     tên cookie từ dòng DEBUG_TOKEN_MAP (hoặc Authorization) vào .auth/auth_config.json → bearer_cookie,
     hoặc copy token vào .auth/bearer.txt.

  2) Cloudflare: cookie cf_clearance gắn với User-Agent của FlareSolverr — POST GraphQL mặc định dùng UA đó.
     Đặt UPWORK_UA khác sẽ dễ bị HTML «Challenge» (403).

  3) Trình duyệt thật: TLS / ngữ cảnh same-origin khác requests thuần; phần còn lại giải quyết bằng (1)+(2).

Cấu hình .auth/: storage_state.json, auth_config.json (flaresolverr_url, warm_url, bearer_cookie, …).
Biến môi trường UPWORK_*, FLARESOLVERR_* ghi đè file.

Luồng: đọc .auth → FlareSolverr GET warm_url → merge cookie → resolve Authorization → POST GraphQL.

  UPWORK_GRAPHQL_BODY=/path/to/body.json — ghi đè file body.
  UPWORK_GRAPHQL_REFERER — mặc định Referer giống log capture (khác warm_url nếu cần).

Exit: 3 = HTTP >=400 ; 4 = HTTP 200 nhưng body có GraphQL errors.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import uuid
import warnings

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")
from pathlib import Path
from typing import Any, Dict, List

try:
    import requests
except ImportError:
    print("pip install requests", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth_loader import (
    describe_authorization_source,
    load_merged_auth,
    parse_cookie_header,
    resolve_authorization_header,
)

BODY_PATH = ROOT / "postman_userJobSearch_body.json"
BODY_MINIMAL_PATH = ROOT / "postman_userJobSearch_body.minimal.json"
GRAPHQL = "https://www.upwork.com/api/graphql/v1?alias=userJobSearch"

# Referer trùng captures/debug_session_20260326_071440.log (request userJobSearch). UPWORK_GRAPHQL_REFERER ghi đè.
_GRAPHQL_REFERER_FROM_CAPTURE = (
    "https://www.upwork.com/nx/search/jobs/"
    "?from_recent_search=true&q=spring%20boot&page=2"
)

# Khi POST bằng requests + cookie CF từ FlareSolverr: User-Agent phải trùng UA mà FlareSolverr dùng khi giải
# challenge — nếu không Cloudflare trả HTML «Challenge - Upwork» (403). Xem README FlareSolverr.
_DEFAULT_FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


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
    """Gần request userJobSearch thành công từ Brave/Chromium (DevTools)."""
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
    """Giống log capture (063705): vnd-eo-* — một số route GraphQL kỳ vọng có trace/visitor."""
    ck = parse_cookie_header(cookie_header)
    visitor = ck.get("visitor_id", "").strip()
    span = str(uuid.uuid4())
    parent = str(uuid.uuid4())
    # Mẫu trace trong log: 9e242565f33b861d-PDX
    trace = f"{secrets.token_hex(8)}-PDX"
    h: Dict[str, str] = {
        "vnd-eo-span-id": span,
        "vnd-eo-parent-span-id": parent,
        "vnd-eo-trace-id": trace,
    }
    if visitor:
        h["vnd-eo-visitorid"] = visitor
    return h


def main() -> None:
    auth_dir = ROOT / ".auth"
    try:
        auth = load_merged_auth(auth_dir)
    except Exception as exc:
        print(f"Lỗi đọc .auth: {exc}", file=sys.stderr)
        print(f"  Cần: {auth_dir / 'storage_state.json'} (chạy capture_user_job_search.py)", file=sys.stderr)
        raise SystemExit(1)

    fs = auth["flaresolverr_url"]
    warm_url = auth["warm_url"]
    cookie_user = auth["cookie"]
    tenant = auth["tenant_id"]

    body_file = _resolve_body_path()
    if not body_file.is_file():
        print(f"Thiếu file body GraphQL: {body_file}", file=sys.stderr)
        raise SystemExit(1)
    if body_file == BODY_PATH:
        print(
            "[0] Body: postman_userJobSearch_body.json "
            "(query + variables như captures/debug_session_20260326_071440.log)",
            flush=True,
        )
    else:
        print(f"[0] Body JSON: {body_file.name}", flush=True)

    body_json = body_file.read_text(encoding="utf-8")
    timeout_ms = int(os.environ.get("FLARESOLVERR_TIMEOUT_MS", "120000"))

    api = f"{fs}/v1"
    print(f"[1] FlareSolverr GET {warm_url!r} …", flush=True)
    r = requests.post(
        api,
        json={
            "cmd": "request.get",
            "url": warm_url,
            "maxTimeout": timeout_ms,
        },
        timeout=(10, timeout_ms // 1000 + 30),
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        print(f"FlareSolverr failed: {data}", file=sys.stderr)
        raise SystemExit(2)

    sol = data.get("solution") or {}
    fs_cookies = sol.get("cookies") or []
    fs_ua = (sol.get("userAgent") or "").strip()
    # Mặc định: UA từ FlareSolverr (khớp cf_clearance). UPWORK_UA= ghi đè; UPWORK_USE_FLARE_UA=0 dùng UA cố định.
    if os.environ.get("UPWORK_UA", "").strip():
        ua = os.environ["UPWORK_UA"].strip()
    elif os.environ.get("UPWORK_USE_FLARE_UA", "1").strip().lower() in ("0", "false", "no", "off"):
        ua = _DEFAULT_FALLBACK_UA
    else:
        ua = fs_ua or _DEFAULT_FALLBACK_UA

    merged = _merge_cookies(cookie_user, fs_cookies)
    print(f"[2] Đã merge {len(fs_cookies)} cookie từ FlareSolverr vào phiên của bạn.", flush=True)

    # Bearer phải khớp cookie GraphQL (*sb trong storage) — tính lại từ cookie sau merge
    merged_cookies = parse_cookie_header(merged)
    authorization = resolve_authorization_header(merged_cookies, auth_dir)
    src = describe_authorization_source(merged_cookies, auth_dir)
    print(f"[2b] Authorization ← {src} (trên cookie đã merge)", flush=True)

    referer = (
        os.environ.get("UPWORK_GRAPHQL_REFERER", "").strip()
        or _GRAPHQL_REFERER_FROM_CAPTURE
    )
    headers = {
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
    # sec-ch-ua Brave/Chrome giả có thể lệch UA thật của FlareSolverr → chỉ gửi khi bật rõ ràng
    if os.environ.get("UPWORK_BROWSER_HINTS", "").strip().lower() in ("1", "true", "yes", "on"):
        headers.update(_browser_client_hints_headers())
    headers.update(_upwork_edge_headers(merged))
    if os.environ.get("UPWORK_GRAPHQL_MINIMAL_HEADERS", "").strip() in ("1", "true", "yes"):
        for k in list(_browser_client_hints_headers().keys()):
            headers.pop(k, None)

    print(f"[3] POST GraphQL (User-Agent = FlareSolverr: {bool(fs_ua and ua == fs_ua)}) …", flush=True)
    gr = requests.post(
        GRAPHQL,
        headers=headers,
        data=body_json.encode("utf-8"),
        timeout=120,
    )
    print(f"HTTP {gr.status_code}", flush=True)
    graphql_errors = False
    ct = gr.headers.get("content-type", "") or ""
    body_preview = (gr.text or "")[:800]
    if gr.status_code == 403 and "Challenge" in body_preview and "text/html" in ct:
        print(
            "\nCloudflare/Upwork trả trang «Challenge» (HTML), không phải JSON GraphQL.\n"
            "Nguyên nhân thường gặp: cookie cf_clearance gắn với User-Agent của FlareSolverr — script đã dùng "
            "UA từ FlareSolverr mặc định. Nếu bạn đặt UPWORK_UA khác, bỏ hoặc dùng cùng UA FlareSolverr.\n"
            "Cách khác: POST GraphQL qua cùng session FlareSolverr (request.post + session) hoặc gọi API trong Playwright.",
            file=sys.stderr,
        )
    if ct.startswith("application/json"):
        try:
            out = gr.json()
            if isinstance(out, dict) and out.get("errors"):
                graphql_errors = True
                print("GraphQL errors (không có data hoặc thiếu quyền field):", flush=True)
                err0 = (out.get("errors") or [{}])[0]
                msg = str((err0 or {}).get("message") or "")
                if "oAuth2 client does not have permission" in msg:
                    print(
                        "\nGợi ý: Bearer phải trùng đúng cookie OAuth cho request này — trong capture log, "
                        "DEBUG_TOKEN_MAP ghi rõ tên cookie (vd. 25897f9esb, có thể là *esb chứ không phải *fsb). "
                        f"Nguồn Bearer hiện tại: {src}. Đặt bearer_cookie hoặc .auth/bearer.txt khớp log/DevTools.\n",
                        file=sys.stderr,
                    )
            text = json.dumps(out, indent=2, ensure_ascii=False)
            print(text[:20000])
            if len(text) > 20000:
                print("\n… (truncated in terminal)", flush=True)
        except json.JSONDecodeError:
            print(gr.text[:8000])
    else:
        print(gr.text[:8000])

    if gr.status_code >= 400:
        raise SystemExit(3)
    if graphql_errors:
        raise SystemExit(4)


if __name__ == "__main__":
    main()
