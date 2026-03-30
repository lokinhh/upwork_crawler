"""
Đọc cấu hình từ thư mục .auth/ (cùng cấp script):
  - storage_state.json  (Playwright — bắt buộc để có cookie + token)
  - auth_config.json      (tuỳ chọn: flaresolverr_url, warm_url, bearer_cookie, …)

Bearer suy từ cookie: *fsb và *esb cùng hạng; cookie dạng chỉ số+sb (vd. 16366163sb) bị loại khỏi tự chọn.
Nên đặt bearer_cookie trong auth_config.json theo DEBUG_TOKEN_MAP trong capture log.
Biến môi trường UPWORK_*, FLARESOLVERR_* (nếu đã set) ghi đè giá trị từ file.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_AUTH_DIR = Path(__file__).resolve().parent / ".auth"

_OAUTH2_STRICT = re.compile(r"^oauth2v2_int_[a-f0-9]{32}$")
# vd. 16366163sb — kết thúc "sb" nhưng không phải client GraphQL *fsb/*esb; không tự dùng làm Bearer
_DIGIT_ONLY_SB = re.compile(r"^[0-9]+sb$", re.IGNORECASE)

# Fallback sau cookie *sb — oauth2_global_js_token thường là client khác scope, dễ lỗi
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
    # *fsb và *esb cùng hạng. Các cookie *sb khác (hex+sb, vd. ab4bffa2sb) rank 1 — sau fsb/esb.
    if s.endswith("fsb") or s.endswith("esb"):
        return 0
    if s.endswith("sb"):
        return 1
    return 99


def _pick_bearer_cookie_and_value(cookies: Dict[str, str], config: Dict[str, Any]) -> tuple[Optional[str], str]:
    """
    Trả về (tên cookie hoặc None nếu bearer_full), và giá trị token (không có tiền tố Bearer).
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
        "Không suy ra được Bearer từ cookies. Thêm vào .auth/auth_config.json "
        "một trong: bearer_full, bearer_cookie, hoặc bearer_cookie_priority."
    )


def _pick_bearer_value(cookies: Dict[str, str], config: Dict[str, Any]) -> str:
    return _pick_bearer_cookie_and_value(cookies, config)[1]


def describe_authorization_source(
    cookies: Dict[str, str],
    auth_dir: Optional[Path] = None,
) -> str:
    """
    Mô tả nguồn Bearer (để debug), không in toàn bộ token.
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
    Khi DEBUG_TOKEN_MAP có nhiều cookie cùng giá trị Bearer — chọn tên phù hợp GraphQL
    (cùng quy tắc _graphql_oauth_cookie_rank: *fsb/*esb trước, loại dạng chỉ số+sb).
    """
    if not candidates:
        raise ValueError("candidates rỗng")
    uniq = list(dict.fromkeys(candidates))
    ranked = sorted(uniq, key=lambda n: (_graphql_oauth_cookie_rank(n), str(n)))
    return ranked[0]


def merge_auth_config_bearer_cookie(
    cookie_name: str,
    auth_dir: Optional[Path] = None,
) -> bool:
    """
    Ghi/merge `bearer_cookie` vào `auth_dir/auth_config.json`; setdefault flaresolverr_url / warm_url nếu thiếu.
    Trả về True nếu nội dung file thay đổi.
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
        "Không có current_organization_uid trong cookie và không có tenant_id trong auth_config.json"
    )


def load_auth_config(auth_dir: Optional[Path] = None) -> Dict[str, Any]:
    base = Path(auth_dir) if auth_dir else _AUTH_DIR
    path = base / "auth_config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_cookie_header(header: str) -> Dict[str, str]:
    """Chuỗi Cookie header -> dict name -> value."""
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
    Tính header Authorization từ map cookie (vd. sau khi merge với FlareSolverr).

    Thứ tự: UPWORK_AUTHORIZATION (env) > .auth/bearer.txt > auth_config + _pick_bearer_value.
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
    Trả về:
      authorization  — header Authorization (có tiền tố Bearer )
      cookie         — chuỗi Cookie
      tenant_id
      flaresolverr_url
      warm_url
    """
    base = Path(auth_dir) if auth_dir else _AUTH_DIR
    storage_path = base / "storage_state.json"
    if not storage_path.is_file():
        raise FileNotFoundError(f"Thiếu {storage_path} — chạy capture_user_job_search.py để tạo.")

    config = load_auth_config(base)

    raw = json.loads(storage_path.read_text(encoding="utf-8"))
    cookies = _cookies_dict_from_storage(raw)
    bearer_val = _pick_bearer_value(cookies, config)
    auth_header = f"Bearer {bearer_val}"
    cookie_header = _cookie_header(cookies)
    tenant = _tenant_id(cookies, config)

    # File một dòng — copy nguyên từ DevTools → Network → userJobSearch → Authorization (khi log đã redact)
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

    # Env ghi đè (ưu tiên sau cùng)
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
