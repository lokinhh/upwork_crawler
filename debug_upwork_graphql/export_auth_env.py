#!/usr/bin/env python3
"""
Print out `export VAR=...` (shell) lines from .auth/storage_state.json (+ auth_config.json).
Use: eval "$(python3 export_auth_env.py)"

Environment variables set before running are still kept; The script only prints the merged value
(auth_loader prioritizes env file overwriting — should run without export).
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
