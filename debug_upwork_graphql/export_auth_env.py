#!/usr/bin/env python3
"""
In ra các dòng `export VAR=...` (shell) từ .auth/storage_state.json (+ auth_config.json).
Dùng:  eval "$(python3 export_auth_env.py)"

Biến môi trường đã set trước khi chạy vẫn được giữ; script chỉ in giá trị đã merge
(auth_loader ưu tiên env ghi đè file — nên chạy khi chưa export).
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth_loader import load_merged_auth


def main() -> None:
    try:
        a = load_merged_auth()
    except Exception as exc:
        print(f"# export_auth_env: {exc}", file=sys.stderr)
        raise SystemExit(1)

    mapping = [
        ("UPWORK_AUTHORIZATION", a["authorization"]),
        ("UPWORK_COOKIE", a["cookie"]),
        ("UPWORK_TENANT_ID", a["tenant_id"]),
        ("FLARESOLVERR_URL", a["flaresolverr_url"]),
        ("UPWORK_WARM_URL", a["warm_url"]),
    ]
    for name, val in mapping:
        print(f"export {name}={shlex.quote(val)}")


if __name__ == "__main__":
    main()
