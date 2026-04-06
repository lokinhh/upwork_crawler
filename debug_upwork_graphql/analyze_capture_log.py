#!/usr/bin/env python3
"""
Analyze debug_session_*.log file from capture_user_job_search.py.

- If Authorization is <redacted> → cannot infer token from log; need:
  - rerun with DEBUG_LOG_SENSITIVE=1 (local machine only), or
  - copy Authorization from DevTools → .auth/bearer.txt

Usage:
  python analyze_capture_log.py captures/debug_session_20260326_063705.log
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python analyze_capture_log.py <path/to/debug_session_*.log>", file=sys.stderr)
        raise SystemExit(2)
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"Unreadable: {path}", file=sys.stderr)
        raise SystemExit(1)

    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = text.split("userJobSearch REQUEST")
    print(f"File: {path} ({len(text)} chars)\n")
    print(f"Number of userJobSearch blocks: {len(blocks) - 1}\n")

    redacted = 0
    samples = 0
    for i, chunk in enumerate(blocks[1:], start=1):
        if samples >= 3:
            break
        # Find JSON headers after line ---
        m = re.search(
            r"--- Request headers[^\n]*---\s*\n(\{.*?\n\})",
            chunk,
            re.DOTALL,
        )
        if not m:
            continue
        samples += 1
        raw = m.group(1)
        try:
            hdrs = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[block {i}] Could not parse JSON headers\n")
            continue
        auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
        if "redacted" in str(auth).lower():
            redacted += 1
            print(f"[block {i}] authorization: REDACTED — failed to get token from this log.")
        else:
            print(f"[block {i}] authorization: valid (length {len(auth)}) — do not print here.")
        print(f"         referer: {hdrs.get('referer', hdrs.get('Referer', ''))[:90]}")
        print(f"         x-upwork-api-tenantid: {hdrs.get('x-upwork-api-tenantid', '')}")
        print(f"         user-agent: {(hdrs.get('user-agent') or '')[:70]}…")
        print()

    if redacted:
        print(
            "→ This log has hidden Authorization (safe default)."
            "To match token to browser:\n"
            "  1) DevTools → Network → userJobSearch → copy header Authorization\n"
            " 2) Write to debug_upwork_graphql/.auth/bearer.txt (one line)\n"
            " Or run capture with: DEBUG_LOG_SENSITIVE=1 python capture_user_job_search.py\n"
        )
    if samples == 0:
        print("No Request headers JSON block for userJobSearch found in file.")


if __name__ == "__main__":
    main()
