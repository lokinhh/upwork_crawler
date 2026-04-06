#!/usr/bin/env python3
"""
Check response from GET Upwork search: status, headers, body snippet.
Run: python scripts/check_upwork_response.py
Useful to determine whether 403 comes from Cloudflare or Upwork/other causes.
"""
import sys
from pathlib import Path

# Repo root directory.
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

    # Hints
    body_lower = body.lower()
    if "cloudflare" in body_lower or "cf-bypass" in body_lower or "just a moment" in body_lower or "checking your browser" in body_lower:
        print("\n>>> Likely CLOUDFLARE (challenge/checking browser page).")
    elif "access denied" in body_lower or "blocked" in body_lower or "forbidden" in body_lower:
        print("\n>>> Likely Upwork/backend block (403 Forbidden).")
    if "server" in resp.headers:
        sv = resp.headers["server"]
        if "cloudflare" in sv.lower():
            print("\n>>> Server header indicates CLOUDFLARE.")


if __name__ == "__main__":
    main()
