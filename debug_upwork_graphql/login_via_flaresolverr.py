#!/usr/bin/env python3
"""
Test đăng nhập Upwork qua FlareSolverr + Playwright (Chromium thật, không dùng curl_cffi/requests tới Upwork).

1) FlareSolverr GET trang login → cookie (cf_clearance, …) + userAgent
2) Playwright: viewport + add_init_script (webdriver) + cookie FlareSolverr → page.goto
2b) Trích **iovation** / **forterToken** từ trang (script + meta); env ghi đè — nếu vẫn trống, dán UPWORK_IOVATION từ DevTools (POST login bước password)
3) Đăng nhập: mặc định **fetch** (API 2 bước); hoặc **UPWORK_LOGIN_FORM=1** — điền form trên trang (giống user thật, iovation gửi kèm request thật)
4) Tuỳ chọn: GET subordinate JS, POST visitor-gql-token

Không cần .auth/storage_state.json; cần FlareSolverr + Playwright (chromium).

Bắt buộc (env):
  UPWORK_EMAIL
  UPWORK_PASSWORD

File (tuỳ chọn): login.env — copy từ login.env.example
  UPWORK_LOGIN_ENV — đường dẫn file env khác

Tuỳ chọn:
  UPWORK_PLAYWRIGHT_HEADLESS — mặc định 1; 0 = mở cửa sổ (debug)
  UPWORK_PLAYWRIGHT_VIEWPORT — mặc định 1280x900 (W×H)
  UPWORK_PLAYWRIGHT_GOTO_WAIT — domcontentloaded | load | networkidle (mặc định networkidle; có thể đổi nếu chờ lâu)
  UPWORK_IOVATION / UPWORK_FORTER_TOKEN
  UPWORK_SUBORDINATE_CLIENT_ID
  UPWORK_LOGIN_WARM_URL
  FLARESOLVERR_TIMEOUT_MS
  UPWORK_FETCH_SUBORDINATE / UPWORK_FETCH_VISITOR_GQL
  UPWORK_LOGIN_FORM — 1 = đăng nhập bằng fill/click form (không dùng fetch API)
  UPWORK_LOGIN_FORM_TIMEOUT_MS — mặc định 90000
  UPWORK_LOGIN_FORM_SUCCESS_GLOB — mặc định **/nx/** (Playwright glob sau khi login)

Debug: UPWORK_LOGIN_DEBUG, UPWORK_LOGIN_DEBUG_LOG (như trước)
Console: UPWORK_LOGIN_VERBOSE — mặc định 1 (in thêm status URL, tóm tắt từng bước); 0 = chỉ dòng [n] chính

Exit: 0 OK | 1 thiếu env | 2 FlareSolverr lỗi | 3 login thất bại | 4 HTTP/lỗi khác
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import warnings
from pathlib import Path
from typing import Any, Dict, IO, List, Optional, TextIO
from urllib.parse import unquote

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

try:
    import requests as std_requests
except ImportError:
    print("pip install requests", file=sys.stderr)
    raise

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("pip install playwright && playwright install chromium", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOGIN_PAGE = "https://www.upwork.com/ab/account-security/login"
AUTH_ORIGIN = "https://auth.upwork.com"
_DEFAULT_SUBORDINATE_ID = "ad40656599b41c597ebc81ca2e09a677"
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

_TOKEN_IN_JS = re.compile(r'"token":"(oauth2v2_int_[a-f0-9]{32})"')

_DEBUG_BODY_PREVIEW = 24_000


def _debug_enabled() -> bool:
    return os.environ.get("UPWORK_LOGIN_DEBUG", "1").strip().lower() not in ("0", "false", "no", "off")


def _debug_log_path() -> Path:
    custom = (os.environ.get("UPWORK_LOGIN_DEBUG_LOG") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return ROOT / f"login_debug_{time.strftime('%Y%m%d_%H%M%S')}.log"


def _verbose_login() -> bool:
    return os.environ.get("UPWORK_LOGIN_VERBOSE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _log_v(msg: str) -> None:
    if _verbose_login():
        print(msg, flush=True)


def _login_response_summary(d: Any) -> Dict[str, Any]:
    """Tóm tắt JSON phản hồi API login (không đụng password)."""
    if not isinstance(d, dict):
        return {"_type": type(d).__name__, "preview": repr(d)[:500]}
    out: Dict[str, Any] = {}
    for k in ("success", "mode", "eventCode", "vendor", "reason"):
        if k in d:
            out[k] = d.get(k)
    if "userNid" in d:
        out["userNid"] = str(d.get("userNid"))[:96]
    if "userUid" in d:
        out["userUid"] = d.get("userUid")
    ru = d.get("redirectUrl")
    if isinstance(ru, str) and ru.strip():
        out["redirectUrl_prefix"] = ru.strip()[:160]
    al = d.get("alerts")
    if al is not None:
        try:
            s = json.dumps(al, ensure_ascii=False)
            out["alerts"] = s[:4000] + ("…" if len(s) > 4000 else "")
        except TypeError:
            out["alerts"] = repr(al)[:2000]
    return out


def _print_login_failure_hint(d: Any, step: str) -> None:
    if not isinstance(d, dict):
        print(f"[{step}] Phản hồi không phải object JSON: {type(d).__name__}", file=sys.stderr)
        return
    print(
        f"[{step}] success={d.get('success')!r} mode={d.get('mode')!r} "
        f"eventCode={d.get('eventCode')!r}",
        file=sys.stderr,
    )
    al = d.get("alerts")
    if al is not None:
        try:
            print(f"[{step}] alerts: {json.dumps(al, ensure_ascii=False)[:2000]}", file=sys.stderr)
        except Exception:
            print(f"[{step}] alerts: {repr(al)[:2000]}", file=sys.stderr)


def _fetch_console_line(label: str, r: Dict[str, Any]) -> None:
    if not _verbose_login():
        return
    st = int(r.get("status") or 0)
    url = (str(r.get("url") or ""))[:220]
    ct = (str(r.get("contentType") or ""))[:100]
    bl = len(str(r.get("text") or ""))
    print(f"    [{label}] HTTP {st} body_len={bl} url={url!r} ct={ct!r}", flush=True)


def _redact_cookie_val(val: str, keep: int = 6) -> str:
    v = str(val)
    if len(v) <= keep * 2:
        return "(short)"
    return f"{v[:keep]}…{v[-keep:]} (len={len(v)})"


def _sanitize_flaresolver_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    sol = out.get("solution")
    if isinstance(sol, dict):
        sol = dict(sol)
        resp = sol.get("response")
        if isinstance(resp, str) and len(resp) > 4000:
            sol["response"] = f"[truncated len={len(resp)}]\n{resp[:4000]}\n…"
        out["solution"] = sol
    return out


def _response_body_text(resp: Any) -> str:
    t = getattr(resp, "text", None)
    if callable(t):
        return t() or ""
    if isinstance(t, str):
        return t
    return ""


def _response_headers_dict(resp: Any) -> Dict[str, str]:
    hp = getattr(resp, "headers", None)
    if hp is None:
        return {}
    if hasattr(hp, "items"):
        return {str(k): str(v) for k, v in hp.items()}
    return {}


def _write_fetch_dump(
    fp: IO[str],
    label: str,
    r: Dict[str, Any],
    body_max: int = _DEBUG_BODY_PREVIEW,
) -> None:
    status = int(r.get("status") or 0)
    url = str(r.get("url") or "")
    ct = str(r.get("contentType") or "")
    fp.write(f"\n### {label} (fetch trong trang)\n")
    fp.write(f"url (final): {url}\n")
    fp.write(f"status_code: {status}\n")
    fp.write(f"content-type: {ct}\n")
    body = str(r.get("text") or "")
    fp.write(f"body_len: {len(body)}\n")
    fp.write("body_preview:\n")
    fp.write(body[:body_max])
    if len(body) > body_max:
        fp.write(f"\n… [truncated, total {len(body)}]\n")
    else:
        fp.write("\n")
    low = body.lower()
    if "challenge" in low and "html" in ct.lower():
        fp.write(
            "\n(note) Có dấu hiệu trang Challenge HTML — cookie cf_clearance phải khớp UA/phiên.\n"
        )


def _write_http_dump(
    fp: IO[str],
    label: str,
    resp: Any,
    body_max: int = _DEBUG_BODY_PREVIEW,
) -> None:
    status = getattr(resp, "status_code", None)
    if status is None:
        status = getattr(resp, "status", 0)
    url = getattr(resp, "url", "")
    fp.write(f"\n### {label}\n")
    fp.write(f"url (final): {url}\n")
    fp.write(f"status_code: {status}\n")
    hdrs = _response_headers_dict(resp)
    fp.write("response_headers:\n")
    for k, v in sorted(hdrs.items()):
        fp.write(f"  {k}: {v}\n")
    body = _response_body_text(resp)
    fp.write(f"body_len: {len(body)}\n")
    fp.write("body_preview:\n")
    fp.write(body[:body_max])
    if len(body) > body_max:
        fp.write(f"\n… [truncated, total {len(body)}]\n")
    else:
        fp.write("\n")
    ct = (hdrs.get("content-type") or hdrs.get("Content-Type") or "").lower()
    if "challenge" in body.lower() and "html" in ct:
        fp.write(
            "\n(note) Có dấu hiệu trang Challenge HTML — cookie cf_clearance phải khớp UA/phiên.\n"
        )


class LoginDebugLog:
    """Ghi log chi tiết ra file. path=None → không ghi (no-op)."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._fp: Optional[TextIO] = None

    def __enter__(self) -> "LoginDebugLog":
        if self.path is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("w", encoding="utf-8")
        self._fp.write("login_via_flaresolverr debug log\n")
        self._fp.write(f"started: {time.strftime('%Y-%m-%dT%H:%M:%S')}Z\n")
        self._fp.write(f"python: {sys.version}\n")
        self._fp.flush()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._fp:
            self._fp.close()
            self._fp = None

    def active(self) -> bool:
        return self._fp is not None

    def line(self, msg: str) -> None:
        if self._fp:
            self._fp.write(msg.rstrip() + "\n")
            self._fp.flush()

    def section(self, title: str) -> None:
        self.line(f"\n--- {title} ---")

    def json_block(self, label: str, obj: Any) -> None:
        if not self._fp:
            return
        self._fp.write(f"\n### {label}\n")
        try:
            self._fp.write(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
        except TypeError:
            self._fp.write(repr(obj))
        self._fp.write("\n")
        self._fp.flush()

    def http_response(self, label: str, resp: Any, body_max: int = _DEBUG_BODY_PREVIEW) -> None:
        if not self._fp:
            return
        _write_http_dump(self._fp, label, resp, body_max=body_max)

    def fetch_result(self, label: str, r: Dict[str, Any], body_max: int = _DEBUG_BODY_PREVIEW) -> None:
        if not self._fp:
            return
        _write_fetch_dump(self._fp, label, r, body_max=body_max)

    def login_api_summary(self, label: str, d: Any) -> None:
        if not self._fp:
            return
        self._fp.write(f"\n### {label}\n")
        try:
            self._fp.write(
                json.dumps(_login_response_summary(d), indent=2, ensure_ascii=False, default=str)
            )
        except Exception:
            self._fp.write(repr(d)[:8000])
        self._fp.write("\n")
        self._fp.flush()


def _load_login_env_file() -> None:
    custom = (os.environ.get("UPWORK_LOGIN_ENV") or "").strip()
    path = Path(custom).expanduser().resolve() if custom else ROOT / "login.env"
    if not path.is_file():
        return
    print(f"[0] Đọc biến môi trường từ {path.name}", flush=True)
    raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        if not key:
            continue
        if key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


def _normalize_samesite(val: Any) -> str:
    if val is None:
        return "Lax"
    s = str(val).strip().lower()
    if s in ("strict",):
        return "Strict"
    if s in ("lax", "", "unspecified"):
        return "Lax"
    if s in ("none",):
        return "None"
    return "Lax"


def _flaresolver_to_playwright_cookies(fs_cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Chuyển cookie FlareSolverr → định dạng context.add_cookies."""
    out: List[Dict[str, Any]] = []
    for c in fs_cookies:
        name = c.get("name")
        if not name:
            continue
        val = c.get("value")
        if val is None:
            continue
        domain = (c.get("domain") or "").strip()
        if not domain:
            domain = ".upwork.com"
        path = (c.get("path") or "/").strip() or "/"
        out.append(
            {
                "name": str(name),
                "value": str(val),
                "domain": domain,
                "path": path,
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": _normalize_samesite(c.get("sameSite")),
            }
        )
    return out


def _load_flaresolverr_url() -> str:
    p = ROOT / ".auth" / "auth_config.json"
    if p.is_file():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            u = (cfg.get("flaresolverr_url") or "").strip()
            if u:
                return u.rstrip("/")
        except json.JSONDecodeError:
            pass
    return (os.environ.get("FLARESOLVERR_URL", "").strip() or "http://localhost:8191").rstrip("/")


def _csrf_from_playwright(context: Any) -> str:
    for c in context.cookies():
        if c.get("name") == "XSRF-TOKEN":
            return unquote(str(c.get("value") or ""))
    return ""


def _csrf_token(page: Any, context: Any) -> str:
    """CSRF: ưu tiên cookie jar; fallback document.cookie (giống trình duyệt)."""
    raw = _csrf_from_playwright(context)
    if raw:
        return raw
    try:
        v = page.evaluate(
            """() => {
              const m = document.cookie.match(/(?:^|;\\s*)XSRF-TOKEN=([^;]+)/);
              return m ? decodeURIComponent(m[1].trim()) : '';
            }"""
        )
        return unquote(str(v or ""))
    except Exception:
        return ""


_PLAYWRIGHT_WEBDRIVER_PATCH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _parse_viewport() -> Dict[str, int]:
    raw = (os.environ.get("UPWORK_PLAYWRIGHT_VIEWPORT") or "").strip()
    if raw and "x" in raw.lower():
        part = raw.lower().replace("×", "x").split("x")
        return {"width": int(part[0].strip()), "height": int(part[1].strip())}
    return {"width": 1280, "height": 900}


def _goto_wait_option() -> str:
    w = (os.environ.get("UPWORK_PLAYWRIGHT_GOTO_WAIT") or "").strip().lower()
    if w in ("domcontentloaded", "load", "networkidle", "commit"):
        return w
    return "networkidle"


def _headers_sec_fetch_cross_site() -> Dict[str, str]:
    return {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


_FETCH_IN_PAGE_JS = """
async (args) => {
  const { url, method, headers, body } = args;
  const init = { method: method || 'GET', credentials: 'include' };
  const h = Object.assign({}, headers || {});
  if (body !== undefined && body !== null) {
    init.body = typeof body === 'string' ? body : JSON.stringify(body);
    if (!h['Content-Type'] && !h['content-type']) {
      h['Content-Type'] = 'application/json';
    }
  }
  if (Object.keys(h).length) init.headers = h;
  const resp = await fetch(url, init);
  const ct = resp.headers.get('content-type') || '';
  const text = await resp.text();
  let json = null;
  try {
    if (ct.includes('application/json') || /^\\s*[\\[{]/.test(text)) {
      json = JSON.parse(text);
    }
  } catch (e) {}
  return {
    status: resp.status,
    ok: resp.ok,
    url: resp.url,
    contentType: ct,
    text: text,
    json: json,
  };
}
"""


_EXTRACT_LOGIN_FRAUD_TOKENS_JS = """
() => {
  let iovation = '';
  let forter = '';
  const scripts = document.querySelectorAll('script');
  for (const script of scripts) {
    const content = script.textContent || script.innerText || '';
    if (!content.includes('iovation')) continue;
    let match = content.match(/iovation["']?\\s*[:=]\\s*["']([^"']{200,})["']/);
    if (match && match[1].length > 200) {
      iovation = match[1];
      break;
    }
    match = content.match(/"iovation"\\s*:\\s*"([^"\\\\]+)"/);
    if (match && match[1].length > 200) {
      iovation = match[1];
      break;
    }
  }
  if (!iovation) {
    try {
      const w = window;
      if (w._iovation) {
        if (typeof w._iovation === 'string') {
          iovation = w._iovation;
        } else if (w._iovation.blackbox) {
          iovation = w._iovation.blackbox;
        } else if (w._iovation.token) {
          iovation = w._iovation.token;
        }
      }
    } catch (e) {}
  }
  if (!iovation) {
    const meta = document.querySelector('meta[name="iovation"], meta[property="iovation"]');
    if (meta) iovation = meta.getAttribute('content') || '';
  }
  if (!iovation) {
    for (const script of scripts) {
      const content = script.textContent || script.innerText || '';
      if (!content.includes('iovation')) continue;
      let m = content.match(/iovation["']?\\s*[:=]\\s*["']([^"']{50,})["']/);
      if (m) { iovation = m[1]; break; }
      m = content.match(/"iovation"\\s*:\\s*"([^"\\\\]+)"/);
      if (m && m[1].length >= 50) { iovation = m[1]; break; }
    }
  }
  try {
    const ck = document.cookie.match(/(?:^|;\\s*)forterToken=([^;]+)/);
    if (ck) forter = decodeURIComponent(ck[1].trim());
  } catch (e) {}
  if (!forter) {
    try {
      if (window.forterToken) forter = String(window.forterToken);
    } catch (e) {}
  }
  return { iovation: iovation || '', forter: forter || '' };
}
"""


def _extract_login_fraud_tokens_from_page(page: Any) -> Dict[str, str]:
    try:
        r = page.evaluate(_EXTRACT_LOGIN_FRAUD_TOKENS_JS)
        if isinstance(r, dict):
            return {
                "iovation": str(r.get("iovation") or "").strip(),
                "forter": str(r.get("forter") or "").strip(),
            }
    except Exception:
        pass
    return {"iovation": "", "forter": ""}


def _abs_upwork_url(u: str) -> str:
    u = u.strip()
    if u.startswith("/"):
        return "https://www.upwork.com" + u
    return u


def _fetch_in_page(page: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    return page.evaluate(_FETCH_IN_PAGE_JS, args)


def _login_headers_json(csrf: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "x-odesk-csrf-token": csrf,
    }


def _json_from_fetch(r: Dict[str, Any]) -> Any:
    j = r.get("json")
    if j is not None:
        return j
    t = (r.get("text") or "").strip()
    if not t:
        return None
    return json.loads(t)


def _use_form_login() -> bool:
    return os.environ.get("UPWORK_LOGIN_FORM", "").strip().lower() in ("1", "true", "yes", "on")


def _attach_login_post_sniffer(page: Any, dbg: LoginDebugLog) -> None:
    """Bắt POST /ab/account-security/login để log mode + độ dài iovation (debug)."""

    def on_request(request: Any) -> None:
        try:
            if request.method != "POST":
                return
            u = str(request.url or "")
            if "/ab/account-security/login" not in u:
                return
            pd = request.post_data
            if not pd:
                return
            data = json.loads(pd)
            login = data.get("login") or {}
            mode = login.get("mode")
            iov = login.get("iovation")
            if iov:
                dbg.line(f"sniff POST login: mode={mode!r} iovation_len={len(str(iov))}")
            if _verbose_login() and iov:
                print(f"    [sniff] iovation prefix: {str(iov)[:72]}…", flush=True)
        except Exception:
            pass

    page.on("request", on_request)


def _login_via_playwright_form(
    page: Any,
    email: str,
    password: str,
    dbg: LoginDebugLog,
) -> Dict[str, Any]:
    """Điền email → submit → đợi ô password → điền → submit; đợi URL thành công."""
    form_timeout = int(os.environ.get("UPWORK_LOGIN_FORM_TIMEOUT_MS", "90000"))
    success_glob = (os.environ.get("UPWORK_LOGIN_FORM_SUCCESS_GLOB") or "").strip() or "**/nx/**"

    dbg.section("login qua form (fill + click)")
    dbg.line(f"success_glob={success_glob!r} timeout_ms={form_timeout}")

    username_selectors = (
        'input[name="username"]',
        'input[type="email"]',
        "#login_username",
        'input[autocomplete="username"]',
        'input[name="login[username]"]',
    )
    user_filled = False
    for sel in username_selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=15_000)
            loc.fill(email)
            dbg.line(f"form: username — selector {sel!r}")
            user_filled = True
            break
        except Exception as exc:
            dbg.line(f"form: bỏ qua selector {sel!r}: {exc}")
            continue
    if not user_filled:
        print("Không tìm thấy ô username (thử đổi selector hoặc UPWORK_PLAYWRIGHT_HEADLESS=0).", file=sys.stderr)
        raise SystemExit(3)

    submit_selectors = (
        'button[data-ev-label="continue"]:visible',
        "button:has-text('Continue'):visible",
        "button:has-text('Log In'):visible",
        'button[type="submit"]:visible',
    )

    def _click_submit(step: str) -> bool:
        for sel in submit_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() < 1:
                    dbg.line(f"form: {step} — không thấy selector {sel!r}")
                    continue
                if not loc.is_visible():
                    dbg.line(f"form: {step} — selector không visible {sel!r}")
                    continue
                loc.click(timeout=10_000)
                dbg.line(f"form: {step} — click {sel!r}")
                return True
            except Exception as exc:
                dbg.line(f"form: {step} — không click được {sel!r}: {exc}")
        return False
    clicked1 = _click_submit("bước 1")
    if not clicked1:
        page.keyboard.press("Enter")
        dbg.line("form: bước 1 — gửi Enter")

    pwd_selectors = (
        'input[name="password"]',
        'input[type="password"]',
        "#login_password",
        'input[autocomplete="current-password"]',
    )
    try:
        page.wait_for_selector(
            ",".join(pwd_selectors),
            state="visible",
            timeout=form_timeout,
        )
    except Exception as exc:
        dbg.line(f"form: không thấy ô password: {exc}")
        print("Không thấy ô password sau bước email — CAPTCHA hoặc selector đổi.", file=sys.stderr)
        raise SystemExit(3) from exc

    pwd_filled = False
    for sel in pwd_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() < 1:
                continue
            loc.fill(password)
            dbg.line(f"form: password — selector {sel!r}")
            pwd_filled = True
            break
        except Exception as exc:
            dbg.line(f"form: password thử {sel!r}: {exc}")
            continue
    if not pwd_filled:
        print("Không điền được ô password.", file=sys.stderr)
        raise SystemExit(3)

    clicked2 = _click_submit("bước 2")
    if not clicked2:
        page.keyboard.press("Enter")
        dbg.line("form: bước 2 — gửi Enter")

    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception as exc:
        dbg.line(f"form: wait_for_load_state(domcontentloaded) lỗi (bỏ qua): {exc}")

    # Đợi theo nhiều trạng thái, tránh timeout mù nếu Upwork đổi luồng điều hướng.
    t0 = time.time()
    blocker_reason = ""
    blocker_selectors = (
        ("2fa_email_code", "text=Check your email"),
        ("2fa_enter_code", "text=Enter the code"),
        ("2fa_otp_input", 'input[name*="otp"]'),
        ("2fa_two_step", "text=Two-step verification"),
        ("challenge", "text=/challenge/i"),
        ("captcha", "iframe[title*='captcha']"),
        ("wrong_password", "text=/incorrect password|wrong password/i"),
        ("account_locked", "text=/account.*locked|temporarily unavailable/i"),
    )
    success_url_tokens = (
        "/nx/",
        "/find-work/",
        "/ab/jobs/search",
        "/n/find-work",
    )

    while (time.time() - t0) * 1000 < form_timeout:
        cur = (page.url or "").lower()
        if "/ab/account-security/login" not in cur and "login" not in cur:
            dbg.line(f"form: thành công — đã rời trang login, url={page.url}")
            break
        if any(tok in cur for tok in success_url_tokens):
            dbg.line(f"form: thành công — URL match token, url={page.url}")
            break
        try:
            if page.url:
                page.wait_for_url(success_glob, timeout=500)
                dbg.line(f"form: thành công — khớp success_glob={success_glob!r}, url={page.url}")
                break
        except Exception:
            pass
        for reason, sel in blocker_selectors:
            try:
                if page.locator(sel).first.is_visible():
                    blocker_reason = reason
                    dbg.line(f"form: phát hiện trạng thái trung gian/lỗi: {reason} ({sel})")
                    break
            except Exception:
                continue
        if blocker_reason:
            break
        page.wait_for_timeout(500)
    else:
        dbg.line(f"form: timeout chờ chuyển trang, url_hiện_tại={page.url}")
        blocker_reason = blocker_reason or "timeout_no_state_change"

    if blocker_reason:
        ts = time.strftime("%Y%m%d_%H%M%S")
        shot = ROOT / f"login_form_stuck_{ts}.png"
        html = ROOT / f"login_form_stuck_{ts}.html"
        try:
            page.screenshot(path=str(shot), full_page=True)
            dbg.line(f"form: đã chụp screenshot: {shot}")
        except Exception as exc:
            dbg.line(f"form: screenshot lỗi: {exc}")
        try:
            html.write_text(page.content(), encoding="utf-8")
            dbg.line(f"form: đã lưu HTML: {html}")
        except Exception as exc:
            dbg.line(f"form: lưu HTML lỗi: {exc}")
        print(
            f"Đăng nhập form chưa hoàn tất ({blocker_reason}) — URL hiện tại: {page.url}. "
            f"Đã lưu snapshot để debug.",
            file=sys.stderr,
        )
        raise SystemExit(3)

    dbg.line(f"form: url cuối={page.url}")
    return {
        "success": 1,
        "userUid": None,
        "redirectUrl": page.url,
        "mode": "form_ui",
    }


def _env_snapshot_for_log(email: str) -> Dict[str, Any]:
    dom = email.split("@")[-1] if "@" in email else ""
    masked = f"{email[:2]}***@{dom}" if email else ""
    return {
        "UPWORK_EMAIL": masked,
        "UPWORK_PASSWORD": "(set)" if os.environ.get("UPWORK_PASSWORD") else "(empty)",
        "UPWORK_LOGIN_WARM_URL": (os.environ.get("UPWORK_LOGIN_WARM_URL") or "").strip() or LOGIN_PAGE,
        "FLARESOLVERR_URL": _load_flaresolverr_url(),
        "FLARESOLVERR_TIMEOUT_MS": int(os.environ.get("FLARESOLVERR_TIMEOUT_MS", "120000")),
        "UPWORK_ACCEPT_LANGUAGE": os.environ.get("UPWORK_ACCEPT_LANGUAGE", "en-US"),
        "UPWORK_IOVATION": f"len={len((os.environ.get('UPWORK_IOVATION') or '').strip())}",
        "UPWORK_FORTER_TOKEN": "(set)" if (os.environ.get("UPWORK_FORTER_TOKEN") or "").strip() else "(empty)",
        "UPWORK_PLAYWRIGHT_HEADLESS": os.environ.get("UPWORK_PLAYWRIGHT_HEADLESS", "1"),
        "UPWORK_PLAYWRIGHT_VIEWPORT": (os.environ.get("UPWORK_PLAYWRIGHT_VIEWPORT") or "").strip()
        or "1280x900",
        "UPWORK_PLAYWRIGHT_GOTO_WAIT": _goto_wait_option(),
        "UPWORK_LOGIN_VERBOSE": os.environ.get("UPWORK_LOGIN_VERBOSE", "1"),
        "UPWORK_LOGIN_FORM": os.environ.get("UPWORK_LOGIN_FORM", "0"),
        "UPWORK_LOGIN_FORM_SUCCESS_GLOB": (os.environ.get("UPWORK_LOGIN_FORM_SUCCESS_GLOB") or "").strip()
        or "**/nx/**",
    }


def run() -> None:
    _load_login_env_file()
    email = (os.environ.get("UPWORK_EMAIL") or "").strip()
    password = os.environ.get("UPWORK_PASSWORD") or ""
    if not email or not password:
        print(
            "Thiếu UPWORK_EMAIL hoặc UPWORK_PASSWORD trong môi trường.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    log_path: Optional[Path] = _debug_log_path() if _debug_enabled() else None
    if log_path:
        print(f"[log] Chi tiết debug: {log_path}", flush=True)
    if _verbose_login():
        print(
            "[log] UPWORK_LOGIN_VERBOSE — in thêm: HTTP, url, body_len, tóm tắt login JSON mỗi bước",
            flush=True,
        )

    with LoginDebugLog(log_path) as dbg:
        try:
            _run_login_flow(email, password, dbg)
        except Exception:
            if dbg.active():
                dbg.section("uncaught exception")
                dbg.line(traceback.format_exc())
            raise


def _run_login_flow(email: str, password: str, dbg: LoginDebugLog) -> None:
    fs = _load_flaresolverr_url()
    warm_url = (os.environ.get("UPWORK_LOGIN_WARM_URL") or "").strip() or LOGIN_PAGE
    timeout_ms = int(os.environ.get("FLARESOLVERR_TIMEOUT_MS", "120000"))
    sub_id = (os.environ.get("UPWORK_SUBORDINATE_CLIENT_ID") or "").strip() or _DEFAULT_SUBORDINATE_ID
    iovation_env = (os.environ.get("UPWORK_IOVATION") or "").strip()
    forter_env = (os.environ.get("UPWORK_FORTER_TOKEN") or "").strip()
    headless = os.environ.get("UPWORK_PLAYWRIGHT_HEADLESS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    dbg.section("environment (sanitized)")
    dbg.json_block("env", _env_snapshot_for_log(email))

    api = f"{fs}/v1"
    print(f"[1] FlareSolverr GET {warm_url!r} …", flush=True)
    dbg.line(f"flare_solverr POST {api} cmd=request.get url={warm_url}")
    fs_api = std_requests.post(
        api,
        json={"cmd": "request.get", "url": warm_url, "maxTimeout": timeout_ms},
        timeout=(10, timeout_ms // 1000 + 30),
    )
    dbg.line(f"flare_solverr API HTTP status: {fs_api.status_code}")
    fs_api.raise_for_status()
    try:
        data = fs_api.json()
    except json.JSONDecodeError:
        dbg.section("flare_solverr API body (not JSON)")
        dbg.line((fs_api.text or "")[:8000])
        print("FlareSolverr trả về không phải JSON", file=sys.stderr)
        raise SystemExit(2)

    dbg.json_block("flare_solverr_response_json (sanitized)", _sanitize_flaresolver_payload(data))
    if data.get("status") != "ok":
        print(f"FlareSolverr failed: {data}", file=sys.stderr)
        raise SystemExit(2)

    sol = data.get("solution") or {}
    fs_cookies = sol.get("cookies") or []
    fs_ua = (sol.get("userAgent") or "").strip()
    ua = fs_ua or _DEFAULT_UA

    dbg.section("FlareSolverr solution (cookies redacted)")
    dbg.line(f"userAgent length={len(ua)}")
    dbg.line(ua[:200] + ("…" if len(ua) > 200 else ""))
    for c in fs_cookies:
        name = c.get("name") or "?"
        val = str(c.get("value", ""))
        dbg.line(f"  cookie {name}: {_redact_cookie_val(val)}")

    pw_cookie_list = _flaresolver_to_playwright_cookies(fs_cookies)
    print(
        f"[2] Đã nhận {len(fs_cookies)} cookie từ FlareSolverr; Playwright + UA FlareSolverr.",
        flush=True,
    )

    accept_lang = os.environ.get("UPWORK_ACCEPT_LANGUAGE", "en-US").strip() or "en-US"
    goto_wait = _goto_wait_option()
    viewport = _parse_viewport()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                user_agent=ua,
                locale="en-US",
                viewport=viewport,
                extra_http_headers={"Accept-Language": accept_lang},
            )
            context.add_init_script(_PLAYWRIGHT_WEBDRIVER_PATCH)
            context.add_cookies(pw_cookie_list)
            page = context.new_page()

            dbg.section("Playwright context (sau add_cookies + init_script)")
            dbg.line(f"cookie inject count: {len(pw_cookie_list)}")
            dbg.line(f"viewport: {viewport}")
            dbg.line(f"page.goto wait_until: {goto_wait}")
            dbg.json_block("cookie names FlareSolverr", [c.get("name") for c in fs_cookies])

            print(
                f"[2a] Playwright page.goto wait_until={goto_wait!r} (headless={headless}) …",
                flush=True,
            )
            r0 = page.goto(warm_url, wait_until=goto_wait, timeout=90_000)
            if r0 is None:
                dbg.line("page.goto trả về None (navigation lỗi?)")
                print("Playwright: page.goto thất bại", file=sys.stderr)
                raise SystemExit(4)
            dbg.http_response("GET warm_url — Playwright page.goto (cookie FlareSolverr)", r0)
            if r0.status >= 400:
                dbg.line(f"FAIL: GET login HTTP {r0.status}")
                print(f"GET login page HTTP {r0.status}", file=sys.stderr)
                if dbg.path:
                    print(f"Xem chi tiết: {dbg.path}", file=sys.stderr)
                raise SystemExit(4)

            dbg.line(f"page.url sau goto: {page.url}")
            _log_v(f"    page.url: {page.url}")
            _log_v(f"    goto response HTTP: {r0.status}")

            csrf = _csrf_token(page, context)
            dbg.line(f"XSRF-TOKEN (csrf) present: {bool(csrf)} length={len(csrf)}")
            if csrf:
                _log_v(f"    csrf prefix: {csrf[:12]}… (len={len(csrf)})")
            if not csrf:
                dbg.json_block(
                    "context.cookies",
                    [
                        {k: (_redact_cookie_val(str(v)) if k == "value" else v) for k, v in c.items()}
                        for c in context.cookies()
                    ],
                )
                print(
                    "Không thấy XSRF-TOKEN sau goto — thử UPWORK_PLAYWRIGHT_HEADLESS=0 hoặc kiểm tra cookie.",
                    file=sys.stderr,
                )
                if dbg.path:
                    print(f"Xem chi tiết: {dbg.path}", file=sys.stderr)
                raise SystemExit(3)

            print("[2b] Lấy iovation / forterToken từ trang (bổ sung nếu chưa có trong env) …", flush=True)
            dbg.section("iovation / forter — trích từ trang")
            try:
                page_tok = _extract_login_fraud_tokens_from_page(page)
                iovation_page = page_tok.get("iovation") or ""
                forter_page = page_tok.get("forter") or ""
            except Exception as exc:
                dbg.line(f"trích token từ trang lỗi: {exc}")
                iovation_page, forter_page = "", ""
            iovation = iovation_env or iovation_page
            forter = forter_env or forter_page
            dbg.line(
                f"iovation: env len={len(iovation_env)} page len={len(iovation_page)} → dùng len={len(iovation)}"
            )
            dbg.line(
                f"forter: env len={len(forter_env)} page len={len(forter_page)} → dùng len={len(forter)}"
            )
            if not iovation:
                dbg.line(
                    "(cảnh báo) Không có iovation sau env+trang — bước password (fetch) thường trả wrongPassword."
                )
                if not _use_form_login():
                    print(
                        "⚠️ Không có iovation (UPWORK_IOVATION và trang đều trống). "
                        "Với fetch API login có thể thất bại — thử UPWORK_LOGIN_FORM=1.",
                        file=sys.stderr,
                    )
            if not forter:
                dbg.line("(gợi ý) forterToken trống — một số tài khoản vẫn cần UPWORK_FORTER_TOKEN.")

            use_form = _use_form_login()
            d2: Dict[str, Any]
            if use_form:
                print("[form] UPWORK_LOGIN_FORM=1 — điền form + click (sniff POST login trong log) …", flush=True)
                _attach_login_post_sniffer(page, dbg)
                d2 = _login_via_playwright_form(page, email, password, dbg)
                dbg.section("Đăng nhập thành công (form UI)")
                dbg.line(f"final_url={page.url}")
                print("✓ Đăng nhập (form).", flush=True)
            if not use_form:
                ts_path = "/ab/account-security/api/timestamp?unit=ms"
                print(f"[3] GET timestamp (fetch trong trang) …", flush=True)
                tr = _fetch_in_page(
                    page,
                    {
                        "url": ts_path,
                        "method": "GET",
                        "headers": {"Accept": "*/*"},
                    },
                )
                dbg.fetch_result("GET timestamp", tr)
                _fetch_console_line("GET timestamp", tr)
                if int(tr.get("status") or 0) >= 400:
                    dbg.line(f"GET timestamp thất bại: HTTP {tr.get('status')}")
                    print(f"GET timestamp HTTP {tr.get('status')}", file=sys.stderr)
                    if dbg.path:
                        print(f"Xem chi tiết: {dbg.path}", file=sys.stderr)
                    raise SystemExit(3)
                try:
                    _ts1 = _json_from_fetch(tr)
                    if isinstance(_ts1, dict) and "timestamp" in _ts1:
                        dbg.line(f"GET timestamp (lần 1) server_ms={_ts1.get('timestamp')}")
                        _log_v(f"    server timestamp (ms): {_ts1.get('timestamp')}")
                except Exception as exc:
                    dbg.line(f"GET timestamp (lần 1) không parse JSON: {exc}")
    
                step1_body: Dict[str, Any] = {
                    "login": {
                        "mode": "username",
                        "username": email,
                        "deviceType": "desktop",
                    }
                }
                if iovation:
                    step1_body["login"]["iovation"] = iovation
    
                print("[4] POST login (username) (fetch trong trang) …", flush=True)
                dbg.json_block("POST login step1 body", step1_body)
                s1 = _fetch_in_page(
                    page,
                    {
                        "url": "/ab/account-security/login",
                        "method": "POST",
                        "headers": _login_headers_json(csrf),
                        "body": step1_body,
                    },
                )
                dbg.fetch_result("POST login (username)", s1)
                _fetch_console_line("POST login username", s1)
                st1 = int(s1.get("status") or 0)
                print(f"    HTTP {st1}", flush=True)
                if st1 != 200:
                    dbg.line(f"POST login (username) HTTP != 200: body đầu={(s1.get('text') or '')[:500]!r}")
                    print((s1.get("text") or "")[:2000], file=sys.stderr)
                    if dbg.path:
                        print(f"Xem chi tiết: {dbg.path}", file=sys.stderr)
                    raise SystemExit(3)
                try:
                    d1 = _json_from_fetch(s1)
                except json.JSONDecodeError as e:
                    dbg.line(f"POST login step1 JSON parse: {e}")
                    raise SystemExit(3) from e
                if not isinstance(d1, dict):
                    print("Phản hồi login bước 1 không phải JSON object", file=sys.stderr)
                    raise SystemExit(3)
                dbg.login_api_summary("Bước 1 (username) — phân tích phản hồi", d1)
                print(f"    response keys: {list(d1.keys())}", flush=True)
                _log_v(
                    f"    bước 1: success={d1.get('success')!r} mode={d1.get('mode')!r} "
                    f"eventCode={d1.get('eventCode')!r} userNid={d1.get('userNid')!r}"
                )
                if d1.get("success") != 0 or d1.get("mode") != "password":
                    dbg.login_api_summary("Bước 1 THẤT BẠI — chi tiết", d1)
                    _print_login_failure_hint(d1, "bước 1 (username)")
                    print(json.dumps(d1, indent=2, ensure_ascii=False), file=sys.stderr)
                    raise SystemExit(3)
                user_nid = d1.get("userNid")
                if not user_nid:
                    dbg.login_api_summary("Bước 1 — thiếu userNid", d1)
                    print("Thiếu userNid", file=sys.stderr)
                    raise SystemExit(3)
    
                csrf = _csrf_token(page, context) or csrf
    
                tr2 = _fetch_in_page(
                    page,
                    {
                        "url": ts_path,
                        "method": "GET",
                        "headers": {"Accept": "*/*"},
                    },
                )
                dbg.fetch_result("GET timestamp (trước password)", tr2)
                _fetch_console_line("GET timestamp (trước password)", tr2)
                st2 = int(tr2.get("status") or 0)
                tr2j: Dict[str, Any] = {}
                if st2 == 200:
                    try:
                        j2 = _json_from_fetch(tr2)
                        if isinstance(j2, dict):
                            tr2j = j2
                    except json.JSONDecodeError as e:
                        dbg.line(f"GET timestamp (trước password) JSON: {e}")
    
                if st2 == 200 and isinstance(tr2j, dict) and "timestamp" in tr2j:
                    server_ts = int(tr2j["timestamp"])
                else:
                    server_ts = int(time.time() * 1000)
                    dbg.line(
                        "GET timestamp (trước password): không có trường timestamp — dùng client time làm server_ts"
                    )
    
                t1 = int(time.time() * 1000)
                elapsed_ms = t1 - server_ts
                if elapsed_ms < 0:
                    dbg.line(
                        f"elapsedTime âm ({elapsed_ms} ms): clamp 0 — server_ts={server_ts} t1={t1} (lệch đồng hồ?)"
                    )
                    print(
                        "⚠️ elapsedTime âm — đã gửi 0 (server_ts > t1; thường do lệch đồng hồ).",
                        file=sys.stderr,
                    )
                    elapsed_ms = 0
    
                dbg.line(
                    f"chuẩn bị password: t1_ms={t1} server_ts_ms={server_ts} elapsedTime_ms={elapsed_ms}"
                )
                _log_v(f"    elapsedTime (ms) gửi lên: {elapsed_ms} (t1={t1}, server_ts={server_ts})")
    
                step2_body: Dict[str, Any] = {
                    "login": {
                        "mode": "password",
                        "userNid": user_nid,
                        "password": password,
                        "deviceType": "desktop",
                        "elapsedTime": elapsed_ms,
                    }
                }
                if iovation:
                    step2_body["login"]["iovation"] = iovation
                if forter:
                    step2_body["login"]["forterToken"] = forter
    
                print(
                    f"[5] POST login (password) (fetch trong trang) — iovation={bool(iovation)}, forter={bool(forter)} …",
                    flush=True,
                )
                _s2_log = json.loads(json.dumps(step2_body))
                _s2_log["login"]["password"] = "(redacted)"
                dbg.json_block("POST login step2 body (password redacted)", _s2_log)
                s2 = _fetch_in_page(
                    page,
                    {
                        "url": "/ab/account-security/login",
                        "method": "POST",
                        "headers": _login_headers_json(csrf),
                        "body": step2_body,
                    },
                )
                dbg.fetch_result("POST login (password)", s2)
                _fetch_console_line("POST login password", s2)
                st2p = int(s2.get("status") or 0)
                print(f"    HTTP {st2p}", flush=True)
                if st2p != 200:
                    dbg.line(f"POST login (password) HTTP != 200: body đầu={(s2.get('text') or '')[:500]!r}")
                    print((s2.get("text") or "")[:2000], file=sys.stderr)
                    if dbg.path:
                        print(f"Xem chi tiết: {dbg.path}", file=sys.stderr)
                    raise SystemExit(3)
                try:
                    d2 = _json_from_fetch(s2)
                except json.JSONDecodeError as e:
                    dbg.line(f"POST login step2 JSON parse: {e}")
                    raise SystemExit(3) from e
                if not isinstance(d2, dict):
                    print("Phản hồi login bước 2 không phải JSON object", file=sys.stderr)
                    raise SystemExit(3)
                dbg.login_api_summary("Bước 2 (password) — phân tích phản hồi", d2)
                _log_v(
                    f"    bước 2: success={d2.get('success')!r} eventCode={d2.get('eventCode')!r} "
                    f"userUid={d2.get('userUid')!r}"
                )
                print(json.dumps(d2, indent=2, ensure_ascii=False)[:4000], flush=True)
                if d2.get("success") != 1:
                    dbg.login_api_summary("Bước 2 THẤT BẠI — chi tiết", d2)
                    _print_login_failure_hint(d2, "bước 2 (password)")
                    raise SystemExit(3)
    
                dbg.section("Đăng nhập thành công")
                dbg.line(f"userUid={d2.get('userUid')} redirectUrl={(d2.get('redirectUrl') or '')[:200]}")
                print("✓ Đăng nhập: success=1.", flush=True)

            if not use_form:
                redirect = (d2.get("redirectUrl") or "").strip()
                if redirect:
                    red_url = _abs_upwork_url(redirect)
                    print(f"[6] GET redirect {red_url[:120]}… (fetch trong trang)", flush=True)
                    rd = _fetch_in_page(
                        page,
                        {
                            "url": red_url,
                            "method": "GET",
                            "headers": {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
                        },
                    )
                    dbg.fetch_result("GET redirect after login", rd)

            sub_token: Optional[str] = None
            visitor_token: Optional[str] = None

            if os.environ.get("UPWORK_FETCH_VISITOR_GQL", "").strip().lower() in ("1", "true", "yes", "on"):
                print("[7] POST /ab/account-security/visitor-gql-token (fetch trong trang) …", flush=True)
                vr = _fetch_in_page(
                    page,
                    {
                        "url": "/ab/account-security/visitor-gql-token",
                        "method": "POST",
                        "headers": {
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        "body": {},
                    },
                )
                dbg.fetch_result("POST visitor-gql-token", vr)
                vst = int(vr.get("status") or 0)
                print(f"    HTTP {vst}", flush=True)
                ct_v = (vr.get("contentType") or "").lower()
                if vst == 200 and "application/json" in ct_v:
                    try:
                        vj = _json_from_fetch(vr)
                    except json.JSONDecodeError:
                        vj = None
                    if isinstance(vj, dict):
                        visitor_token = vj.get("accessToken")
                        vj_log = dict(vj)
                        if "accessToken" in vj_log:
                            vj_log["accessToken"] = "(redacted)"
                        dbg.json_block("visitor-gql JSON", vj_log)
                        print(f"    visitor accessToken prefix: {(visitor_token or '')[:40]}…", flush=True)

            if os.environ.get("UPWORK_FETCH_SUBORDINATE", "").strip().lower() in ("1", "true", "yes", "on"):
                q = int(time.time() * 1000)
                sub_url = f"{AUTH_ORIGIN}/api/v3/oauth2/token/subordinate/v3/{sub_id}?{q}"
                print(f"[8] GET subordinate script …", flush=True)
                sr: Any
                try:
                    sr = _fetch_in_page(
                        page,
                        {
                            "url": sub_url,
                            "method": "GET",
                            "headers": {"Accept": "application/javascript"},
                        },
                    )
                    dbg.fetch_result("GET subordinate oauth2 script (fetch)", sr)
                    sst = int(sr.get("status") or 0)
                    print(f"    HTTP {sst}", flush=True)
                    if sst == 200:
                        body = sr.get("text") or ""
                        m = _TOKEN_IN_JS.search(body or "")
                        if m:
                            sub_token = m.group(1)
                            print(f"    subordinate token prefix: {sub_token[:40]}…", flush=True)
                        else:
                            print("    (không khớp pattern token trong body JS)", file=sys.stderr)
                except Exception as exc:
                    dbg.line(f"GET subordinate qua fetch thất bại (thường do CORS), thử page.request: {exc}")
                    sr = page.request.get(
                        sub_url,
                        headers={
                            "Accept": "application/javascript",
                            "Referer": "https://www.upwork.com/",
                            **_headers_sec_fetch_cross_site(),
                        },
                        timeout=30_000,
                    )
                    dbg.http_response("GET subordinate oauth2 script (page.request fallback)", sr)
                    print(f"    HTTP {sr.status}", flush=True)
                    if sr.status == 200:
                        body = sr.text()
                        m = _TOKEN_IN_JS.search(body or "")
                        if m:
                            sub_token = m.group(1)
                            print(f"    subordinate token prefix: {sub_token[:40]}…", flush=True)
                        else:
                            print("    (không khớp pattern token trong body JS)", file=sys.stderr)

            print("\n--- Tóm tắt ---", flush=True)
            print(f"userUid: {d2.get('userUid')}", flush=True)
            if visitor_token:
                print("visitor-gql accessToken: đã lấy", flush=True)
            if sub_token:
                print("subordinate oauth2v2_int (trong JS): đã lấy", flush=True)
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except std_requests.RequestException as exc:
        print(f"HTTP lỗi (requests/FlareSolverr): {exc}", file=sys.stderr)
        raise SystemExit(4)
    except Exception as exc:
        print(f"Lỗi (Playwright / mạng): {exc}", file=sys.stderr)
        raise SystemExit(4)
