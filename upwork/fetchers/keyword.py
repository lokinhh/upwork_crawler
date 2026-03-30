"""Chuẩn hoá keyword / URL search Upwork → chuỗi `userQuery` cho GraphQL."""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse


def user_query_from_search_keyword(keyword: str) -> str:
    """
    - URL đầy đủ hoặc path `/nx/search/jobs?...` → lấy tham số `q` nếu có, không thì rỗng → fallback URL gốc.
    - Chuỗi thường → strip nguyên văn.
    """
    kw = (keyword or "").strip()
    if not kw:
        return ""
    if kw.startswith("http://") or kw.startswith("https://"):
        p = urlparse(kw)
        qs = parse_qs(p.query)
        for key in ("q", "query"):
            if key in qs and qs[key]:
                return unquote(qs[key][0]).strip()
        return kw
    if kw.startswith("/nx/search/jobs"):
        p = urlparse("https://www.upwork.com" + kw)
        qs = parse_qs(p.query)
        if "q" in qs and qs["q"]:
            return unquote(qs["q"][0]).strip()
    return kw
