#!/usr/bin/env python3
"""
Debug: open Brave (real window), log in manually, then initiate POST
  https://www.upwork.com/api/graphql/v1?alias=userJobSearch

Script **does not “count” or retrieve tokens** — OAuth2 token (`oauth2v2_int_…`) issued by **Upwork web app**
(JS/Nuxt) placed in **cookie** (and sometimes used in memory) after login; when calling GraphQL,
fetch/XHR attaches the `Authorization: Bearer …` header. We just **observe** the sent request.
To know which Bearer matches **which cookie name**: turn on `DEBUG_TOKEN_MAP=1` — will log (safely) the cookie name
matches the Bearer value, does not print the full token.

Login session is saved to file storage_state (cookie + localStorage)
so you don't need to log in again next time (unless Upwork expires the session).

Run:
  cd debug_upwork_graphql
  python -m venv .venv && source .venv/bin/activate # options
  pip install -r requirements.txt
  python capture_user_job_search.py

Environment variables:
  BRAVE_EXECUTABLE       — binary Brave
  UPWORK_START_URL — opening page
  UPWORK_STORAGE_STATE — JSON Playwright file (default: .auth/storage_state.json)
  UPWORK_DEBUG_LOG — detailed log file: optional path; leave blank = create your own
                           captures/debug_session_<UTC_ts>.log ; 0/off = do not write file
  SAVE_REQUEST_JSON — "1" adds a separate request JSON file in captures/
  STORAGE_SAVE_INTERVAL — seconds, save sessions periodically (default 120, 0 = off)
  DEBUG_UPWORK_CONSOLE — "1" prints console.log from page
  DEBUG_LOG_SENSITIVE — "1" prints cookies/authorization (commit log is strongly not recommended)
  DEBUG_TOKEN_MAP — "1" when userJobSearch exists: log cookie name matching Bearer + localStorage hint (no token printed)
  UPWORK_AUTO_AUTH_CONFIG — enabled by default when DEBUG_TOKEN_MAP=1: write bearer_cookie to .auth/auth_config.json
                           from exact results (prefer *fsb/*esb). Set 0/false to disable.
  UPWORK_PAGE_CLOSE_TIMEOUT_MS — wait for tab to close (ms). 0 = unlimited (default).
                           Playwright defaults to 30 seconds if not set → easily timeout while still using the browser.

Do not commit captures/ and .auth/ — contain sessions and logs.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, TextIO
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Browser, BrowserContext, async_playwright

TARGET_PATH_FRAGMENT = "/api/graphql/v1"
TARGET_ALIAS = "userJobSearch"

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(msg: str) -> None:
    line = f"[{_now_iso()}] {msg}"
    print(line, flush=True)
    if _log_sink:
        _log_sink.write(line + "\n")
        _log_sink.flush()


def detail(text: str, *, stdout_limit: int = 24_000) -> None:
    """Full write to log file; stdout can be truncated (default 24k characters)."""
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
            + f"\n... [stdout truncated {len(text) - stdout_limit} chars; full text in log file]",
            flush=True,
        )


def _resolve_debug_log_path() -> Optional[Path]:
    raw = os.environ.get("UPWORK_DEBUG_LOG", "").strip()
    if raw.lower() in ("0", "no", "false", "off", "none"):
        return None
    if raw:
        return Path(raw).expanduser().resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return (CAPTURES / f"debug_session_{stamp}.log").resolve()


def _start_file_log() -> None:
    global _log_sink, _log_path
    path = _resolve_debug_log_path()
    if path is None:
        _log_sink = None
        _log_path = None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_path = path
    _log_sink = open(path, "w", encoding="utf-8")
    _log_sink.write(f"# upwork graphql debug session started {_now_iso()}\n")
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


def is_user_job_search_url(url: str) -> bool:
    parsed = urlparse(url)
    if TARGET_PATH_FRAGMENT not in (parsed.path or ""):
        return False
    alias = parse_qs(parsed.query).get("alias", [])
    return len(alias) == 1 and alias[0] == TARGET_ALIAS


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


def _parse_interval() -> float:
    raw = os.environ.get("STORAGE_SAVE_INTERVAL", "120").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 120.0


def _page_close_timeout_ms() -> float:
    """0 = no timeout (wait for tab closure indefinitely). Default 0 — avoids Playwright's 30s error."""
    raw = os.environ.get("UPWORK_PAGE_CLOSE_TIMEOUT_MS", "0").strip()
    try:
        v = float(raw)
        return max(0.0, v)
    except ValueError:
        return 0.0


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _auto_auth_config_enabled() -> bool:
    """Default on; UPWORK_AUTO_AUTH_CONFIG=0|false|no|off to not write .auth/auth_config.json."""
    raw = os.environ.get("UPWORK_AUTO_AUTH_CONFIG", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


async def _log_localstorage_oauth_keys(page) -> None:
    """Print a list of localStorage keys that appear to be oauth/token related (key names only)."""
    try:
        keys = await page.evaluate(
            """() => {
              const out = [];
              try {
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i);
                  if (k && /oauth|token|auth|session|bearer/i.test(k)) out.push(k);
                }
              } catch (e) {}
              return out;
            }"""
        )
        log(f"[DEBUG_TOKEN_MAP] localStorage keys (filter oauth|token|auth|session|bearer): {keys}")
    except Exception as exc:
        log(f"[DEBUG_TOKEN_MAP] localStorage scan failed: {exc}")


async def _log_bearer_cookie_map(request, context) -> None:
    """Match the Authorization Bearer value with the cookie in context (log the cookie name only)."""
    h = request.headers
    auth = h.get("authorization") or h.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        log("[DEBUG_TOKEN_MAP] does not have Authorization: Bearer … on this request")
        return
    token = auth[7:].strip()
    cookies = await context.cookies()
    exact = [c["name"] for c in cookies if (c.get("value") or "") == token]
    contains = [c["name"] for c in cookies if token and token in (c.get("value") or "")]
    if exact:
        log(f"[DEBUG_TOKEN_MAP] Bearer matches **raw** cookie values: {exact}")
        log(
            "[DEBUG_TOKEN_MAP] → Suggestion: set auth_config bearer_cookie or .auth/bearer.txt "
            "by one of the above names if calling an external browser API."
        )
        if _auto_auth_config_enabled():
            try:
                pick = preferred_graphql_bearer_cookie_name(exact)
                auth_dir = ROOT / ".auth"
                if merge_auth_config_bearer_cookie(pick, auth_dir):
                    log(
                        f"[AUTO_AUTH_CONFIG] updated {auth_dir / 'auth_config.json'}"
                        f"— bearer_cookie={pick!r}"
                    )
                else:
                    log(
                        f"[AUTO_AUTH_CONFIG] {auth_dir / 'auth_config.json'} already has bearer_cookie={pick!r} "
                        "(not overwritten)"
                    )
            except Exception as exc:
                log(f"[AUTO_AUTH_CONFIG] not recording auth_config: {exc!r}")
    elif contains:
        log(f"[DEBUG_TOKEN_MAP] Bearer is **part** of cookie value (substring): {contains}")
    else:
        log(
            "[DEBUG_TOKEN_MAP] Bearer **doesn't** duplicate any HttpOnly cookies in storage_state — "
            "usually caused by the JS runtime concatenating headers (or tokens in memory)."
            "It is still possible to copy Bearer from this request (DEBUG_LOG_SENSITIVE=1) or Network tab."
        )


async def run() -> None:
    global _log_sink, _log_path

    brave = _default_brave_executable()
    if not brave:
        print(
            "Brave not found. Install Brave or set BRAVE_EXECUTABLE=/path/to/Brave Browser",
            file=sys.stderr,
        )
        raise SystemExit(1)

    CAPTURES.mkdir(parents=True, exist_ok=True)
    _start_file_log()
    if _log_path:
        log(f"detail log file -> {_log_path}")

    start_url = os.environ.get(
        "UPWORK_START_URL",
        "https://www.upwork.com/ab/account-security/login",
    ).strip()
    save_request_files = os.environ.get("SAVE_REQUEST_JSON", "").strip() in ("1", "true", "yes")
    log_console = os.environ.get("DEBUG_UPWORK_CONSOLE", "").strip() in ("1", "true", "yes")
    debug_token_map = _env_truthy("DEBUG_TOKEN_MAP")
    storage_path = _storage_state_path()
    interval = _parse_interval()

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
                    "viewport": {"width": 1280, "height": 800},
                }
                if storage_path.is_file() and storage_path.stat().st_size > 0:
                    ctx_opts["storage_state"] = str(storage_path)
                    log(f"load storage_state from {storage_path} ({_storage_stats(storage_path)})")
                else:
                    log(
                        f"no storage_state at {storage_path} — manual login; "
                        "session will be saved on exit or periodically."
                    )

                context = await browser.new_context(**ctx_opts)
                page = await context.new_page()

                if interval > 0:
                    periodic_task = asyncio.create_task(
                        _periodic_storage_save(context, storage_path, interval, stop_periodic)
                    )
                    log(f"periodic storage save every {interval}s -> {storage_path}")

                token_map_state = {"ls_once": False}

                async def on_request(req) -> None:
                    if req.method != "POST":
                        return
                    if not is_user_job_search_url(req.url):
                        return
                    if debug_token_map:
                        if not token_map_state["ls_once"]:
                            token_map_state["ls_once"] = True
                            await _log_localstorage_oauth_keys(page)
                        await _log_bearer_cookie_map(req, context)
                    ts = _now_iso()
                    sep = "=" * 72
                    block_head = f"\n{sep}\n[{ts}] userJobSearch REQUEST\n{req.url}\n{sep}\n"
                    detail(block_head, stdout_limit=5000)
                    hdrs = _redact_headers(req.headers)
                    hdrs_json = json.dumps(hdrs, indent=2, ensure_ascii=False)
                    detail(
                        "--- Request headers (cookie/auth redacted unless DEBUG_LOG_SENSITIVE=1) ---\n"
                        + hdrs_json,
                        stdout_limit=16_000,
                    )
                    raw_body = req.post_data
                    if not raw_body:
                        detail("(no post body)")
                        return
                    try:
                        body = json.loads(raw_body)
                        body_json = json.dumps(body, indent=2, ensure_ascii=False)
                        detail("--- Request body (JSON, full in log file) ---\n" + body_json)
                        if save_request_files:
                            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                            path = CAPTURES / f"userJobSearch_request_{stamp}.json"
                            path.write_text(body_json, encoding="utf-8")
                            log(f"Saved request JSON -> {path}")
                    except json.JSONDecodeError:
                        detail("--- Request body (raw) ---\n" + raw_body)

                async def on_response(response) -> None:
                    req = response.request
                    if req.method != "POST":
                        return
                    if not is_user_job_search_url(response.url):
                        return
                    ts = _now_iso()
                    sep = "=" * 72
                    block_head = (
                        f"\n{sep}\n[{ts}] userJobSearch RESPONSE status={response.status}\n{sep}\n"
                    )
                    detail(block_head, stdout_limit=4000)
                    rh = dict(response.headers)
                    rh_full = json.dumps(rh, indent=2, ensure_ascii=False)
                    detail("--- Response headers (full) ---\n" + rh_full, stdout_limit=12_000)

                    try:
                        body = await response.json()
                    except Exception as exc:
                        text = await response.text()
                        detail(f"response.json() failed ({exc}); raw body (full in log file):\n{text}")
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                        raw_path = CAPTURES / f"userJobSearch_response_raw_{stamp}.txt"
                        raw_path.write_text(text, encoding="utf-8")
                        log(f"Saved raw -> {raw_path}")
                        return

                    body_json = json.dumps(body, indent=2, ensure_ascii=False)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                    out_path = CAPTURES / f"userJobSearch_response_{stamp}.json"
                    out_path.write_text(body_json, encoding="utf-8")
                    log(f"Saved full response -> {out_path}")
                    detail("--- Response body (JSON, full in log file) ---\n" + body_json)

                page.on("request", on_request)
                page.on("response", on_response)

                def on_frame_nav(frame) -> None:
                    if frame.page != page or frame.parent_frame:
                        return
                    log(f"navigate commit: {frame.url}")

                page.on("framenavigated", on_frame_nav)

                if log_console:

                    def on_console(msg) -> None:
                        log(f"browser console [{msg.type}]: {msg.text}")

                    page.on("console", on_console)

                log(f"Brave: {brave}")
                log(f"open: {start_url}")
                log("Go to Job search to trigger userJobSearch; JSON response in captures/")
                if debug_token_map:
                    log(
                        "DEBUG_TOKEN_MAP=1 — will log cookie names matching Bearer (not print token)."
                        "Script does not generate tokens; tokens are set by Upwork site cookies/JS."
                    )
                    if _auto_auth_config_enabled():
                        log(
                            "UPWORK_AUTO_AUTH_CONFIG (default on): when exact Bearer↔cookie exists,"
                            "write bearer_cookie to .auth/auth_config.json (off: UPWORK_AUTO_AUTH_CONFIG=0)."
                        )
                log("Exit: close tab/window or Ctrl+C (session is still saved in finally).\n")

                await page.goto(start_url, wait_until="domcontentloaded")
                log(f"goto done: {page.url}")

                close_ms = int(_page_close_timeout_ms())
                if close_ms > 0:
                    log(f"waiting for page close (timeout {close_ms} ms)")
                    await page.wait_for_event("close", timeout=close_ms)
                else:
                    log("waiting for page close (no timeout — close tab when done)")
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
                        log(f"final storage_state save failed: {exc}")
                await browser.close()
                log("browser closed")
    finally:
        if _log_path:
            log(f"closing detail log -> {_log_path}")
        _stop_file_log()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
