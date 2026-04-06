"""
Read config from `.auth/` directory (default: project root / `.auth`):
  - storage_state.json  (Playwright - required for cookies + token)
  - auth_config.json    (optional: flaresolverr_url, warm_url, bearer_cookie, ...)

Bearer is inferred from cookies: *fsb and *esb are equal priority; numeric+sb cookies
(e.g. 16366163sb) are excluded from auto-pick.
Set bearer_cookie in auth_config.json based on DEBUG_TOKEN_MAP from capture logs.
Environment variables UPWORK_* and FLARESOLVERR_* (when set) override file values.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

def default_auth_dir() -> Path:
    """Default `.auth` directory: repo root (same level as `upwork` package directory)."""
    # loader.py → auth/ → upwork/ → repo root
    return Path(__file__).resolve().parent.parent.parent / ".auth"


_AUTH_DIR = default_auth_dir()

_OAUTH2_STRICT = re.compile(r"^oauth2v2_int_[a-f0-9]{32}$")
# e.g. 16366163sb - ends with "sb" but not GraphQL client *fsb/*esb; do not auto-use as Bearer.
_DIGIT_ONLY_SB = re.compile(r"^[0-9]+sb$", re.IGNORECASE)

# Fallback after *sb cookies - oauth2_global_js_token is often another client scope and may fail with:
# "Requested oAuth2 client does not have permission to see some of the requested fields"
_DEFAULT_BEARER_FALLBACK: List[str] = [
    "visitor_topnav_gql_token",
    "oauth2_global_js_token",
]


def _cookies_dict_from_storage(data: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in data.get("cookies") or []:
        name = c.get("name")
        if not name:
            continue
        out[str(name)] = str(c.get("value", ""))
    return out


def _cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _graphql_oauth_cookie_rank(name: str) -> int:
    s = str(name).lower()
    if _DIGIT_ONLY_SB.match(s):
        return 99
    # *fsb and *esb have equal rank. Other *sb cookies (hex+sb, e.g. ab4bffa2sb) rank lower.
    if s.endswith("fsb") or s.endswith("esb"):
        return 0
    if s.endswith("sb"):
        return 1
    return 99


def _pick_bearer_cookie_and_value(cookies: Dict[str, str], config: Dict[str, Any]) -> tuple[Optional[str], str]:
    """
    Return (cookie name, or None for bearer_full) and token value (without Bearer prefix).
    """
    if config.get("bearer_full"):
        s = str(config["bearer_full"]).strip()
        if s.lower().startswith("bearer "):
            return None, s[7:].strip()
        return None, s

    name = (config.get("bearer_cookie") or "").strip()
    if name and name in cookies:
        return name, cookies[name]

    priority = config.get("bearer_cookie_priority")
    if isinstance(priority, list) and priority:
        for n in priority:
            if n in cookies:
                v = cookies[n]
                if _OAUTH2_STRICT.match(v) or v.startswith("oauth2v2_int_"):
                    return n, v

    ranked = sorted(
        ((n, v) for n, v in cookies.items() if _graphql_oauth_cookie_rank(n) < 99),
        key=lambda x: (_graphql_oauth_cookie_rank(x[0]), str(x[0])),
    )
    for n, v in ranked:
        if _OAUTH2_STRICT.match(v):
            return n, v

    for n in _DEFAULT_BEARER_FALLBACK:
        if n in cookies:
            v = cookies[n]
            if _OAUTH2_STRICT.match(v) or v.startswith("oauth2v2_int_"):
                return n, v

    for n, v in cookies.items():
        if _OAUTH2_STRICT.match(v):
            return n, v

    for n, v in cookies.items():
        if v.startswith("oauth2v2_int_"):
            return n, v

    raise ValueError(
        "Cannot infer Bearer from cookies. Add one of these to .auth/auth_config.json: "
        "bearer_full, bearer_cookie, or bearer_cookie_priority."
    )


def _pick_bearer_value(cookies: Dict[str, str], config: Dict[str, Any]) -> str:
    return _pick_bearer_cookie_and_value(cookies, config)[1]


def describe_authorization_source(
    cookies: Dict[str, str],
    auth_dir: Optional[Path] = None,
) -> str:
    """
    Describe Bearer source for debugging without printing full token.
    """
    base = Path(auth_dir) if auth_dir else _AUTH_DIR
    if os.environ.get("UPWORK_AUTHORIZATION", "").strip():
        return "env:UPWORK_AUTHORIZATION"

    bearer_file = base / "bearer.txt"
    if bearer_file.is_file():
        raw_b = bearer_file.read_text(encoding="utf-8").strip()
        if raw_b and not os.environ.get("UPWORK_AUTHORIZATION", "").strip():
            return f"file:{bearer_file.name}"

    config = load_auth_config(base)
    try:
        ck, _val = _pick_bearer_cookie_and_value(cookies, config)
    except ValueError:
        return "unknown"
    if ck is None:
        return "auth_config:bearer_full"
    return f"cookie:{ck}"


def preferred_graphql_bearer_cookie_name(candidates: List[str]) -> str:
    """
    When DEBUG_TOKEN_MAP has multiple cookies with the same Bearer value, choose
    the GraphQL-friendly name (same ranking rules as _graphql_oauth_cookie_rank).
    """
    if not candidates:
        raise ValueError("candidates is empty")
    uniq = list(dict.fromkeys(candidates))
    ranked = sorted(uniq, key=lambda n: (_graphql_oauth_cookie_rank(n), str(n)))
    return ranked[0]


def merge_auth_config_bearer_cookie(
    cookie_name: str,
    auth_dir: Optional[Path] = None,
) -> bool:
    """
    Write/merge `bearer_cookie` into `auth_dir/auth_config.json`; set default
    flaresolverr_url / warm_url if missing. Return True when file content changes.
    """
    name = (cookie_name or "").strip()
    if not name:
        return False
    base = Path(auth_dir) if auth_dir else _AUTH_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / "auth_config.json"
    data: Dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if (data.get("bearer_cookie") or "").strip() == name:
        return False
    data["bearer_cookie"] = name
    data.setdefault("flaresolverr_url", "http://localhost:8191")
    data.setdefault(
        "warm_url",
        "https://www.upwork.com/nx/search/jobs/?q=spring%20boot&page=1",
    )
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def _tenant_id(cookies: Dict[str, str], config: Dict[str, Any]) -> str:
    override = (config.get("tenant_id") or "").strip()
    if override:
        return override
    t = cookies.get("current_organization_uid", "").strip()
    if t:
        return t
    raise ValueError(
        "No current_organization_uid in cookies and no tenant_id in auth_config.json"
    )


def load_auth_config(auth_dir: Optional[Path] = None) -> Dict[str, Any]:
    base = Path(auth_dir) if auth_dir else _AUTH_DIR
    path = base / "auth_config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_cookie_header(header: str) -> Dict[str, str]:
    """Cookie header string -> dict[name] = value."""
    out: Dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def resolve_authorization_header(
    cookies: Dict[str, str],
    auth_dir: Optional[Path] = None,
) -> str:
    """
    Build Authorization header from cookie map (e.g. after FlareSolverr merge).

    Priority: UPWORK_AUTHORIZATION (env) > .auth/bearer.txt > auth_config + _pick_bearer_value.
    """
    base = Path(auth_dir) if auth_dir else _AUTH_DIR

    if os.environ.get("UPWORK_AUTHORIZATION", "").strip():
        a = os.environ["UPWORK_AUTHORIZATION"].strip()
        if not a.lower().startswith("bearer "):
            a = f"Bearer {a}"
        return a

    config = load_auth_config(base)

    bearer_file = base / "bearer.txt"
    if bearer_file.is_file():
        raw_b = bearer_file.read_text(encoding="utf-8").strip()
        if raw_b:
            if raw_b.lower().startswith("bearer "):
                return raw_b
            return f"Bearer {raw_b}"

    bearer_val = _pick_bearer_value(cookies, config)
    return f"Bearer {bearer_val}"


def load_merged_auth(auth_dir: Optional[Path] = None) -> Dict[str, str]:
    """
    Return:
      authorization  - Authorization header (with Bearer prefix)
      cookie         - Cookie string
      tenant_id
      flaresolverr_url
      warm_url
    """
    base = Path(auth_dir) if auth_dir else _AUTH_DIR
    storage_path = base / "storage_state.json"
    if not storage_path.is_file():
        raise FileNotFoundError(
            f"Missing {storage_path} - run automatic login (UPWORK_AUTO_LOGIN) or "
            "`python -m upwork.tools.login_via_flaresolverr` to create storage_state.json."
        )

    config = load_auth_config(base)

    raw = json.loads(storage_path.read_text(encoding="utf-8"))
    cookies = _cookies_dict_from_storage(raw)
    bearer_val = _pick_bearer_value(cookies, config)
    auth_header = f"Bearer {bearer_val}"
    cookie_header = _cookie_header(cookies)
    tenant = _tenant_id(cookies, config)

    # Single-line file - copy directly from DevTools -> Network -> userJobSearch -> Authorization.
    bearer_file = base / "bearer.txt"
    if bearer_file.is_file():
        raw_b = bearer_file.read_text(encoding="utf-8").strip()
        if raw_b and not os.environ.get("UPWORK_AUTHORIZATION", "").strip():
            if raw_b.lower().startswith("bearer "):
                auth_header = raw_b
            else:
                auth_header = f"Bearer {raw_b}"

    flaresolverr = (
        os.environ.get("FLARESOLVERR_URL", "").strip()
        or str(config.get("flaresolverr_url") or "").strip()
        or "http://localhost:8191"
    ).rstrip("/")

    warm = (
        os.environ.get("UPWORK_WARM_URL", "").strip()
        or str(config.get("warm_url") or "").strip()
        or "https://www.upwork.com/nx/search/jobs/?q=spring%20boot&page=1"
    )

    # Env overrides (highest final priority).
    if os.environ.get("UPWORK_AUTHORIZATION", "").strip():
        auth_header = os.environ["UPWORK_AUTHORIZATION"].strip()
        if not auth_header.lower().startswith("bearer "):
            auth_header = f"Bearer {auth_header}"

    if os.environ.get("UPWORK_COOKIE", "").strip():
        cookie_header = os.environ["UPWORK_COOKIE"].strip()

    if os.environ.get("UPWORK_TENANT_ID", "").strip():
        tenant = os.environ["UPWORK_TENANT_ID"].strip()

    if os.environ.get("FLARESOLVERR_URL", "").strip():
        flaresolverr = os.environ["FLARESOLVERR_URL"].strip().rstrip("/")

    if os.environ.get("UPWORK_WARM_URL", "").strip():
        warm = os.environ["UPWORK_WARM_URL"].strip()

    return {
        "authorization": auth_header,
        "cookie": cookie_header,
        "tenant_id": tenant,
        "flaresolverr_url": flaresolverr,
        "warm_url": warm,
    }
