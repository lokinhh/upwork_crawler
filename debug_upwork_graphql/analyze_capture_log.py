#!/usr/bin/env python3
"""
Phân tích file debug_session_*.log từ capture_user_job_search.py.

- Nếu Authorization bị <redacted> → không thể suy ra token từ log; cần:
  - chạy lại với DEBUG_LOG_SENSITIVE=1 (chỉ máy local), hoặc
  - copy Authorization từ DevTools → .auth/bearer.txt

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
        print(f"Không đọc được: {path}", file=sys.stderr)
        raise SystemExit(1)

    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = text.split("userJobSearch REQUEST")
    print(f"File: {path} ({len(text)} chars)\n")
    print(f"Số khối userJobSearch: {len(blocks) - 1}\n")

    redacted = 0
    samples = 0
    for i, chunk in enumerate(blocks[1:], start=1):
        if samples >= 3:
            break
        # Tìm JSON headers sau dòng ---
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
            print(f"[block {i}] Không parse được JSON headers\n")
            continue
        auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
        if "redacted" in str(auth).lower():
            redacted += 1
            print(f"[block {i}] authorization: REDACTED — không lấy được token từ log này.")
        else:
            print(f"[block {i}] authorization: có giá trị (độ dài {len(auth)}) — không in ra đây.")
        print(f"         referer: {hdrs.get('referer', hdrs.get('Referer', ''))[:90]}")
        print(f"         x-upwork-api-tenantid: {hdrs.get('x-upwork-api-tenantid', '')}")
        print(f"         user-agent: {(hdrs.get('user-agent') or '')[:70]}…")
        print()

    if redacted:
        print(
            "→ Log này đã ẩn Authorization (mặc định an toàn). "
            "Để khớp token với trình duyệt:\n"
            "  1) DevTools → Network → userJobSearch → copy header Authorization\n"
            "  2) Ghi vào debug_upwork_graphql/.auth/bearer.txt (một dòng)\n"
            "  Hoặc chạy capture với: DEBUG_LOG_SENSITIVE=1 python capture_user_job_search.py\n"
        )
    if samples == 0:
        print("Không tìm thấy khối Request headers JSON cho userJobSearch trong file.")


if __name__ == "__main__":
    main()
