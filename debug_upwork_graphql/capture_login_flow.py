#!/usr/bin/env python3
"""
Debug flow to log in to Upwork: open Brave (real window), log in manually, capture everything
request/response related to auth (login, token, refresh, cookies, OAuth...).

Automatic:
  - Save storage_state (cookie + localStorage) for Playwright
  - Extract cookie name/bearer_cookie hint (via auth_loader when Bearer matches cookie)
  - Detailed logging + HTML snapshot + manifest script (src + inline has size limit)

Run:
  cd debug_upwork_graphql
  source .venv/bin/activate
  python capture_login_flow.py

Environment variables:
  BRAVE_EXECUTABLE — Brave binary (default found on macOS)
  UPWORK_START_URL — opening page (default: login page)
  UPWORK_STORAGE_STATE — storage_state file (default: .auth/storage_state.json)
  UPWORK_LOGIN_CAPTURE_FRESH — 1 (default): do not load old storage — log in from scratch.
                              0: load storage_state if present (debug refresh/session available)
  UPWORK_DEBUG_LOG — main log file; empty = captures/login_flow_<ts>/session.log
  UPWORK_LOGIN_OUT_DIR — subfolder in captures/ (empty = login_flow_<UTC_ts>)
  UPWORK_LOGIN_HTML_DELAY_MS — after navigate, how long to wait before capturing HTML (default 1500)
  UPWORK_LOGIN_INLINE_MAX_BYTES — Maximum number of bytes each inline script can write (500000)
  UPWORK_LOGIN_BODY_MAX_BYTES — response body auth maximum read/write (524288)
  DEBUG_LOG_SENSITIVE — 1: full cookie/authorization log (no commit log)
  UPWORK_AUTO_AUTH_CONFIG — same as capture_user_job_search: merge bearer_cookie (default on)
  UPWORK_PAGE_CLOSE_TIMEOUT_MS — 0 = wait for tab close indefinitely
  STORAGE_SAVE_INTERVAL — save storage periodically (seconds), default 60

Do not commit captures/ and .auth/ — contain sessions and logs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO, Tuple
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth_loader import merge_auth_config_bearer_cookie, preferred_graphql_bearer_cookie_name

CAPTURES = ROOT / "captures"
DEFAULT_STORAGE = ROOT / ".auth" / "storage_state.json"

_SENSITIVE_HEADER_KEYS = frozenset(
    k.lower()
    for k in (
        "cookie",
        "authorization",
        "set-cookie",
        "x-xsrf-token",
        "xsrf-token",
    )
)

_log_sink: Optional[TextIO] = None
_log_path: Optional[Path] = None

# --- logging ---


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(msg: str) -> None:
    line = f"[{_now_iso()}] {msg}"
    print(line, flush=True)
    if _log_sink:
        _log_sink.write(line + "\n")
        _log_sink.flush()


def detail(text: str, *, stdout_limit: int = 20_000) -> None:
    if _log_sink:
        _log_sink.write(text)
        if not text.endswith("\n"):
            _log_sink.write("\n")
        _log_sink.flush()
    if len(text) <= stdout_limit:
        print(text, flush=True)
    else:
        print(
            text[:stdout_limit]
            + f"\n... [stdout truncated {len(text) - stdout_limit} chars; see log file]",
            flush=True,
        )


def _start_file_log(path: Path) -> None:
    global _log_sink, _log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_path = path
    _log_sink = open(path, "w", encoding="utf-8")
    _log_sink.write(f"# upwork login flow capture started {_now_iso()}\n")
    _log_sink.flush()


def _stop_file_log() -> None:
    global _log_sink, _log_path
    if _log_sink:
        try:
            _log_sink.write(f"\n# session ended {_now_iso()}\n")
        except OSError:
            pass
        try:
            _log_sink.close()
        except OSError:
            pass
    _log_sink = None
    _log_path = None


def _default_brave_executable() -> Optional[str]:
    env = os.environ.get("BRAVE_EXECUTABLE", "").strip()
    if env:
        return env
    if sys.platform == "darwin":
        p = Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        if p.is_file():
            return str(p)
    for name in ("brave-browser", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _storage_state_path() -> Path:
    raw = os.environ.get("UPWORK_STORAGE_STATE", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_STORAGE.resolve()


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    sensitive = os.environ.get("DEBUG_LOG_SENSITIVE", "").strip() in ("1", "true", "yes")
    if sensitive:
        return dict(headers)
    out: Dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADER_KEYS:
            out[k] = f"<redacted len={len(v)}>"
        else:
            out[k] = v
    return out


def _storage_stats(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        return "empty"
    try:
        data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        origins = data.get("origins") or []
        return f"cookies={len(cookies)} origins={len(origins)}"
    except Exception as exc:
        return f"unreadable ({exc})"


async def _save_storage(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))
    log(f"storage_state saved -> {path} ({_storage_stats(path)})")


def _parse_interval() -> float:
    raw = os.environ.get("STORAGE_SAVE_INTERVAL", "60").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def _page_close_timeout_ms() -> float:
    raw = os.environ.get("UPWORK_PAGE_CLOSE_TIMEOUT_MS", "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _auto_auth_config_enabled() -> bool:
    raw = os.environ.get("UPWORK_AUTO_AUTH_CONFIG", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _body_max_bytes() -> int:
    return max(4096, _int_env("UPWORK_LOGIN_BODY_MAX_BYTES", 524_288))


def _inline_max_bytes() -> int:
    return max(1024, _int_env("UPWORK_LOGIN_INLINE_MAX_BYTES", 500_000))


def _html_delay_ms() -> int:
    return max(0, _int_env("UPWORK_LOGIN_HTML_DELAY_MS", 1500))


# --- auth URL heuristics ---

_AUTH_SUBSTRINGS = (
    "oauth",
    "openid",
    "/token",
    "token?",
    "refresh_token",
    "access_token",
    "/login",
    "/signin",
    "sign-in",
    "/auth",
    "/session",
    "/identity",
    "/sso",
    "/authorize",
    "account-security",
    "mfa",
    "two-factor",
    "2fa",
    "credential",
    "password",
    "challenge",
    "xsrf",
    "csrf",
    "bearer",
    "grant_type",
    "redirect_uri",
    "code_challenge",
    "well-known",
    "userinfo",
    "revoke",
    "logout",
    "signout",
)

_AUTH_HOST_HINTS = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "appleid.apple.com",
    "facebook.com",
    "oauth",
    "auth.",
)


def _host_looks_auth(host: str) -> bool:
    h = (host or "").lower()
    return any(x in h for x in _AUTH_HOST_HINTS)


def is_auth_related_url(url: str) -> bool:
    """Heuristic: URL appears to be related to auth/SSO/token."""
    u = (url or "").lower()
    if not u:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if _host_looks_auth(p.netloc or ""):
        return True
    for s in _AUTH_SUBSTRINGS:
        if s in u:
            return True
    path = (p.path or "").lower()
    if "upwork.com" in (p.netloc or "").lower() and any(
        x in path for x in ("/nx/", "/ab/", "/freelancers/", "/signup")
    ):
        # too broad — only if path includes login/account
        if any(x in path for x in ("login", "account", "security", "oauth")):
            return True
    return False


def _set_cookie_auth_related(url: str, header_names: List[str]) -> bool:
    """Response has Set-Cookie on Upwork domain → considered session related."""
    if "set-cookie" not in [x.lower() for x in header_names]:
        return False
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return "upwork.com" in host or "upwork" in host


_TOKEN_JSON_KEYS = frozenset(
    (
        "access_token",
        "refresh_token",
        "id_token",
        "token_type",
        "expires_in",
        "session_token",
        "auth_token",
    )
)


def _scan_json_for_token_keys(obj: Any, found: Set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k).lower()
            if ks in _TOKEN_JSON_KEYS or "token" in ks or "oauth" in ks:
                found.add(str(k))
            _scan_json_for_token_keys(v, found)
    elif isinstance(obj, list):
        for it in obj[:200]:
            _scan_json_for_token_keys(it, found)


def _summarize_json_body(raw: str) -> Tuple[Optional[Dict[str, Any]], Set[str]]:
    """Parse JSON if possible; returns keys with tokens."""
    keys: Set[str] = set()
    try:
        data = json.loads(raw)
    except Exception:
        return None, keys
    _scan_json_for_token_keys(data, keys)
    return data, keys


# --- session dir ---


def _resolve_session_dir() -> Path:
    custom = os.environ.get("UPWORK_LOGIN_OUT_DIR", "").strip()
    if custom:
        return (CAPTURES / custom).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return (CAPTURES / f"login_flow_{stamp}").resolve()


def _slug_from_url(url: str, max_len: int = 80) -> str:
    try:
        p = urlparse(url)
        path = (p.path or "/").replace("/", "_").strip("_") or "root"
    except Exception:
        path = "url"
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", path)[:max_len]
    return slug or "page"


# --- Playwright hooks ---


class LoginCaptureState:
    def __init__(self, session_dir: Path, context: BrowserContext) -> None:
        self.session_dir = session_dir
        self.context = context
        self.event_seq = 0
        self.seen_bearer_merged = False
        self.manifest_scripts: List[Dict[str, Any]] = []
        self.events_jsonl = session_dir / "events.jsonl"
        self.html_dir = session_dir / "html"
        self.scripts_dir = session_dir / "scripts"
        self.events_dir = session_dir / "events"
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._html_tasks: Set[asyncio.Task[Any]] = set()

    def _next_event_id(self) -> int:
        self.event_seq += 1
        return self.event_seq

    def _append_jsonl(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(self.events_jsonl, "a", encoding="utf-8") as f:
            f.write(line)

    async def _maybe_merge_bearer_from_request(self, request) -> None:
        if self.seen_bearer_merged or not _auto_auth_config_enabled():
            return
        h = request.headers
        auth = h.get("authorization") or h.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return
        token = auth[7:].strip()
        cookies = await self.context.cookies()
        exact = [c["name"] for c in cookies if (c.get("value") or "") == token]
        if not exact:
            return
        try:
            pick = preferred_graphql_bearer_cookie_name(exact)
            auth_dir = ROOT / ".auth"
            if merge_auth_config_bearer_cookie(pick, auth_dir):
                log(f"[AUTO_AUTH_CONFIG] bearer_cookie={pick!r} -> {auth_dir / 'auth_config.json'}")
            self.seen_bearer_merged = True
        except Exception as exc:
            log(f"[AUTO_AUTH_CONFIG] merge failed: {exc!r}")

    async def on_request(self, request) -> None:
        try:
            await self._on_request_impl(request)
        except Exception as exc:
            log(f"on_request error: {exc!r}")

    async def _on_request_impl(self, request) -> None:
        url = request.url
        if not is_auth_related_url(url):
            return
        eid = self._next_event_id()
        hdrs = _redact_headers(dict(request.headers))
        rec: Dict[str, Any] = {
            "type": "request",
            "id": eid,
            "ts": _now_iso(),
            "method": request.method,
            "url": url,
            "resource_type": request.resource_type,
            "headers": hdrs,
        }
        pd = request.post_data
        if pd:
            sensitive = os.environ.get("DEBUG_LOG_SENSITIVE", "").strip() in ("1", "true", "yes")
            if len(pd) > _body_max_bytes():
                rec["post_data_truncated"] = True
                pd_log = pd[:_body_max_bytes()] + "\n... [truncated]"
            else:
                pd_log = pd
            if not sensitive and any(
                x in pd.lower() for x in ("password", "secret", "client_secret", "refresh_token")
            ):
                rec["post_data"] = f"<redacted len={len(pd)} — set DEBUG_LOG_SENSITIVE=1 to log>"
            else:
                rec["post_data"] = pd_log
        path = self.events_dir / f"{eid:04d}_request.json"
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        self._append_jsonl({"event": "request", "id": eid, "url": url})
        sep = "=" * 72
        detail(f"\n{sep}\n[{_now_iso()}] AUTH REQUEST #{eid} {request.method} {url}\n{sep}\n")
        detail(json.dumps(rec, indent=2, ensure_ascii=False), stdout_limit=12_000)
        await self._maybe_merge_bearer_from_request(request)

    async def on_response(self, response) -> None:
        try:
            await self._on_response_impl(response)
        except Exception as exc:
            log(f"on_response error: {exc!r}")

    async def _on_response_impl(self, response) -> None:
        req = response.request
        url = response.url
        header_keys = [k for k in response.headers.keys()]
        auth_url = is_auth_related_url(url)
        cookie_auth = _set_cookie_auth_related(url, header_keys)
        if not auth_url and not cookie_auth:
            return
        eid = self._next_event_id()
        rh = dict(response.headers)
        rh_redacted = _redact_headers(rh)
        rec: Dict[str, Any] = {
            "type": "response",
            "id": eid,
            "ts": _now_iso(),
            "status": response.status,
            "url": url,
            "request_url": req.url,
            "headers": rh_redacted,
            "auth_url_match": auth_url,
            "set_cookie_match": cookie_auth,
        }
        body_preview = ""
        token_keys: Set[str] = set()
        try:
            body = await response.text()
        except Exception as exc:
            rec["body_error"] = str(exc)
            body = ""
        max_b = _body_max_bytes()
        if len(body) > max_b:
            rec["body_truncated"] = True
            body_preview = body[:max_b]
        else:
            body_preview = body
        _, token_keys = _summarize_json_body(body_preview)
        if token_keys:
            rec["json_token_like_keys"] = sorted(token_keys)
        sensitive = os.environ.get("DEBUG_LOG_SENSITIVE", "").strip() in ("1", "true", "yes")
        if sensitive:
            rec["body"] = body_preview
        else:
            rec["body_len"] = len(body)
            if body_preview and len(body_preview) < 8000:
                rec["body_preview_safe"] = body_preview[:2000] + (
                    "..." if len(body_preview) > 2000 else ""
                )
            else:
                rec["body_preview_safe"] = f"<large body len={len(body)} — see events file or DEBUG_LOG_SENSITIVE=1>"
        raw_path = self.events_dir / f"{eid:04d}_response_body.txt"
        if body and sensitive:
            raw_path.write_text(body, encoding="utf-8")
            rec["body_file"] = str(raw_path.relative_to(self.session_dir))
        elif body and token_keys:
            raw_path.write_text(body_preview, encoding="utf-8")
            rec["body_file"] = str(raw_path.relative_to(self.session_dir))
        path = self.events_dir / f"{eid:04d}_response.json"
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        self._append_jsonl({"event": "response", "id": eid, "url": url, "status": response.status})
        sep = "=" * 72
        detail(f"\n{sep}\n[{_now_iso()}] AUTH RESPONSE #{eid} status={response.status} {url}\n{sep}\n")
        detail(json.dumps(rec, indent=2, ensure_ascii=False), stdout_limit=12_000)

    def schedule_page_snapshot(self, page: Page) -> None:
        """Capture HTML + script after delay (SPA)."""
        if page.is_closed():
            return
        url = page.url or ""
        key = f"{id(page)}:{url}"
        delay = _html_delay_ms() / 1000.0

        async def job() -> None:
            try:
                await asyncio.sleep(delay)
                if page.is_closed():
                    return
                await self._dump_page_html_and_scripts(page)
            except Exception as exc:
                log(f"page snapshot error: {exc!r}")

        t = asyncio.create_task(job())
        self._html_tasks.add(t)
        t.add_done_callback(lambda _: self._html_tasks.discard(t))

    async def _dump_page_html_and_scripts(self, page: Page) -> None:
        if page.is_closed():
            return
        url = page.url or "about:blank"
        uh = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        slug = _slug_from_url(url)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        base = f"{stamp}_{uh}_{slug}"

        try:
            html = await page.content()
        except Exception as exc:
            log(f"page.content() failed: {exc!r}")
            return

        html_path = self.html_dir / f"{base}.html"
        html_path.write_text(html, encoding="utf-8")
        log(f"[HTML] saved -> {html_path}")

        inline_max = _inline_max_bytes()
        try:
            script_info = await page.evaluate(
                """() => {
                  const scripts = Array.from(document.scripts);
                  return scripts.map((s, i) => ({
                    index: i,
                    src: s.src || '',
                    type: s.type || '',
                    async: !!s.async,
                    defer: !!s.defer,
                    inlineChars: s.src ? 0 : (s.textContent || '').length
                  }));
                }"""
            )
        except Exception as exc:
            log(f"script enumerate failed: {exc!r}")
            script_info = []

        inline_parts: List[str] = []
        try:
            inlines = await page.evaluate(
                """() => {
                  const scripts = Array.from(document.scripts).filter(s => !s.src);
                  return scripts.map((s, i) => ({ index: i, text: s.textContent || '' }));
                }"""
            )
        except Exception:
            inlines = []

        for block in inlines:
            idx = block.get("index", 0)
            text = block.get("text") or ""
            if not text.strip():
                continue
            truncated = text[:inline_max]
            suf = ".js" if len(text) <= inline_max else ".js.truncated.txt"
            spath = self.scripts_dir / f"{base}_inline_{idx}{suf}"
            spath.write_text(truncated, encoding="utf-8")
            inline_parts.append(
                {
                    "index": idx,
                    "path": str(spath.relative_to(self.session_dir)),
                    "chars": len(text),
                    "written": len(truncated),
                }
            )

        manifest = {
            "ts": _now_iso(),
            "url": url,
            "html_file": str(html_path.relative_to(self.session_dir)),
            "external_scripts": script_info,
            "inline_saved": inline_parts,
        }
        self.manifest_scripts.append(manifest)
        man_path = self.scripts_dir / f"{base}_manifest.json"
        man_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        detail(f"[SCRIPTS] manifest -> {man_path}", stdout_limit=4000)


def _attach_page_nav_logging(page: Page, state: LoginCaptureState) -> None:
    def on_nav(frame) -> None:
        if frame.page != page or frame.parent_frame:
            return
        log(f"navigate commit: {frame.url}")
        state.schedule_page_snapshot(page)

    page.on("framenavigated", on_nav)

    if _env_truthy("DEBUG_UPWORK_CONSOLE", False):

        def on_console(msg) -> None:
            log(f"browser console [{msg.type}]: {msg.text}")

        page.on("console", on_console)


async def run() -> None:
    global _log_sink, _log_path

    brave = _default_brave_executable()
    if not brave:
        print(
            "Brave not found. Install Brave or set BRAVE_EXECUTABLE=",
            file=sys.stderr,
        )
        raise SystemExit(1)

    CAPTURES.mkdir(parents=True, exist_ok=True)
    session_dir = _resolve_session_dir()
    session_dir.mkdir(parents=True, exist_ok=True)

    log_raw = os.environ.get("UPWORK_DEBUG_LOG", "").strip()
    _stop_file_log()
    if log_raw.lower() in ("0", "no", "false", "off", "none"):
        pass
    elif log_raw:
        _start_file_log(Path(log_raw).expanduser().resolve())
    else:
        _start_file_log(session_dir / "session.log")

    log(f"session artifacts -> {session_dir}")
    if _log_path:
        log(f"detail log file -> {_log_path}")

    fresh = _env_truthy("UPWORK_LOGIN_CAPTURE_FRESH", True)
    storage_path = _storage_state_path()
    interval = _parse_interval()

    start_url = os.environ.get(
        "UPWORK_START_URL",
        "https://www.upwork.com/ab/account-security/login",
    ).strip()

    state: Optional[LoginCaptureState] = None
    try:
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(
                headless=False,
                executable_path=brave,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context: Optional[BrowserContext] = None
            stop_periodic = asyncio.Event()
            periodic_task: Optional[asyncio.Task[None]] = None

            try:
                ctx_opts: Dict[str, Any] = {
                    "locale": "en-US",
                    "viewport": {"width": 1280, "height": 900},
                }
                if not fresh and storage_path.is_file() and storage_path.stat().st_size > 0:
                    ctx_opts["storage_state"] = str(storage_path)
                    log(f"load storage_state from {storage_path} ({_storage_stats(storage_path)})")
                else:
                    log(
                        "Manual login mode: does not load storage (UPWORK_LOGIN_CAPTURE_FRESH=0 to load old files)."
                    )

                context = await browser.new_context(**ctx_opts)
                state = LoginCaptureState(session_dir, context)

                def _schedule(coro):
                    try:
                        asyncio.get_running_loop().create_task(coro)
                    except RuntimeError:
                        asyncio.ensure_future(coro)

                context.on("request", lambda r: _schedule(state.on_request(r)))
                context.on("response", lambda r: _schedule(state.on_response(r)))

                page = await context.new_page()
                _attach_page_nav_logging(page, state)

                async def on_new_page(p: Page) -> None:
                    log(f"new page / popup: {p.url}")
                    _attach_page_nav_logging(p, state)

                context.on("page", on_new_page)

                if interval > 0:
                    periodic_task = asyncio.create_task(
                        _periodic_storage_save(context, storage_path, interval, stop_periodic)
                    )
                    log(f"periodic storage save every {interval}s -> {storage_path}")

                log(f"Brave: {brave}")
                log(f"Open: {start_url}")
                log("Manually log in to Upwork (and SSO if available). Close tab when done or Ctrl+C.")
                log(f"HTML + script manifest after each navigate (~{_html_delay_ms()}ms delay).")
                log("Auth requests/responses -> events/ + events.jsonl\n")

                await page.goto(start_url, wait_until="domcontentloaded")
                log(f"goto done: {page.url}")
                state.schedule_page_snapshot(page)

                close_ms = int(_page_close_timeout_ms())
                if close_ms > 0:
                    await page.wait_for_event("close", timeout=close_ms)
                else:
                    await page.wait_for_event("close", timeout=0)
                log("page closed")
            except asyncio.CancelledError:
                log("cancelled (Ctrl+C)")
                raise
            except Exception as exc:
                log(f"unexpected error: {exc!r}")
                raise
            finally:
                stop_periodic.set()
                if periodic_task:
                    periodic_task.cancel()
                    try:
                        await periodic_task
                    except asyncio.CancelledError:
                        pass
                    if context:
                        try:
                            await _save_storage(context, storage_path)
                        except Exception as exc:
                            log(f"final storage_state save failed: {exc!r}")

                    # summary
                    summary_path = session_dir / "login_capture_summary.json"
                    try:
                        st = await context.storage_state()
                        cookies = st.get("cookies") or []
                        names = sorted({str(c.get("name", "")) for c in cookies})
                        oauthish = [
                            n
                            for n in names
                            if any(
                                x in n.lower()
                                for x in ("oauth", "token", "session", "sb", "xsrf", "jwt")
                            )
                        ]
                        summary = {
                            "ended_at": _now_iso(),
                            "storage_state_path": str(storage_path),
                            "cookie_count": len(cookies),
                            "cookie_names": names,
                            "cookie_names_oauthish": oauthish,
                            "scripts_manifest_count": len(state.manifest_scripts),
                        }
                        summary_path.write_text(
                            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                        log(f"summary -> {summary_path}")
                        man_all = session_dir / "scripts" / "all_manifests.json"
                        man_all.write_text(
                            json.dumps(
                                state.manifest_scripts if state else [],
                                indent=2,
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                    except Exception as exc:
                        log(f"summary write failed: {exc!r}")

                await browser.close()
                log("browser closed")
    finally:
        _stop_file_log()


async def _periodic_storage_save(
    context: BrowserContext,
    path: Path,
    interval_sec: float,
    stop: asyncio.Event,
) -> None:
    if interval_sec <= 0:
        await stop.wait()
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
            return
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await _save_storage(context, path)
        except Exception as exc:
            log(f"periodic storage save failed: {exc}")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
