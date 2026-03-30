#!/usr/bin/env python3
"""
Kiểm tra response khi GET Upwork search: status, headers, đoạn body.
Chạy: python scripts/check_upwork_response.py
Để xem 403 do Cloudflare hay Upwork/khác.
"""
import sys
from pathlib import Path

# Thư mục gốc repo
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

def main():
    from curl_cffi import requests as curl_requests

    url = "https://www.upwork.com/nx/search/jobs"
    params = {"q": "python", "sort": "recency"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.upwork.com/",
    }

    print("Request:", url, params)
    print("Impersonate: chrome120")
    print("-" * 60)

    try:
        resp = curl_requests.get(
            url,
            params=params,
            headers=headers,
            impersonate="chrome120",
            timeout=15,
        )
    except Exception as e:
        print("Request failed:", e)
        return

    print("Status:", resp.status_code)
    print("\nResponse headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")

    body = resp.text
    print("\nBody (first 2500 chars):")
    print("-" * 60)
    print(body[:2500])
    print("-" * 60)
    print("Body length:", len(body))

    # Gợi ý
    body_lower = body.lower()
    if "cloudflare" in body_lower or "cf-bypass" in body_lower or "just a moment" in body_lower or "checking your browser" in body_lower:
        print("\n>>> Co the la CLOUDFLARE (trang challenge / checking browser).")
    elif "access denied" in body_lower or "blocked" in body_lower or "forbidden" in body_lower:
        print("\n>>> Co the la Upwork/backend block (403 Forbidden).")
    if "server" in resp.headers:
        sv = resp.headers["server"]
        if "cloudflare" in sv.lower():
            print("\n>>> Header Server cho thay CLOUDFLARE.")


if __name__ == "__main__":
    main()
