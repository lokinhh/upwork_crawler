#!/usr/bin/env python3
"""
Debug: mở Brave (cửa sổ thật), đăng nhập tay, rồi bắt POST
  https://www.upwork.com/api/graphql/v1?alias=userJobSearch

Script **không “tính” hay lấy token** — token OAuth2 (`oauth2v2_int_…`) do **ứng dụng web Upwork**
(JS/Nuxt) đặt vào **cookie** (và đôi khi dùng trong bộ nhớ) sau khi đăng nhập; khi gọi GraphQL,
fetch/XHR gắn header `Authorization: Bearer …`. Ta chỉ **quan sát** request đã gửi.
Để biết Bearer trùng **tên cookie nào**: bật `DEBUG_TOKEN_MAP=1` — sẽ log (an toàn) tên cookie
khớp với giá trị Bearer, không in full token.

Phiên đăng nhập được lưu vào file storage_state (cookie + localStorage)
để lần sau không cần đăng nhập lại (trừ khi Upwork hết hạn phiên).

Chạy:
  cd debug_upwork_graphql
  python -m venv .venv && source .venv/bin/activate   # tùy chọn
  pip install -r requirements.txt
  python capture_user_job_search.py

Biến môi trường:
  BRAVE_EXECUTABLE       — binary Brave
  UPWORK_START_URL       — trang mở đầu
  UPWORK_STORAGE_STATE   — file JSON Playwright (mặc định: .auth/storage_state.json)
  UPWORK_DEBUG_LOG       — file log chi tiết: đường dẫn tùy chọn; để trống = tự tạo
                           captures/debug_session_<UTC_ts>.log ; 0/off = không ghi file
  SAVE_REQUEST_JSON      — "1" ghi thêm file JSON request riêng trong captures/
  STORAGE_SAVE_INTERVAL  — giây, lưu phiên định kỳ (mặc định 120, 0 = tắt)
  DEBUG_UPWORK_CONSOLE   — "1" in console.log từ trang
  DEBUG_LOG_SENSITIVE    — "1" in nguyên cookie/authorization (cực kỳ không nên commit log)
  DEBUG_TOKEN_MAP        — "1" khi có userJobSearch: log tên cookie khớp Bearer + gợi ý localStorage (không in token)
  UPWORK_AUTO_AUTH_CONFIG — mặc định bật khi DEBUG_TOKEN_MAP=1: ghi bearer_cookie vào .auth/auth_config.json
                           từ kết quả exact (ưu tiên *fsb/*esb). Đặt 0/false để tắt.
  UPWORK_PAGE_CLOSE_TIMEOUT_MS — chờ đóng tab (ms). 0 = không giới hạn (mặc định).
                           Playwright mặc định 30s nếu không set → dễ timeout khi còn dùng trình duyệt.

Không commit captures/ và .auth/ — chứa session và log.
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
    """Ghi đầy đủ vào file log; stdout có thể cắt (mặc định 24k ký tự)."""
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
    """0 = không timeout (chờ đóng tab vô hạn). Mặc định 0 — tránh lỗi 30s của Playwright."""
    raw = os.environ.get("UPWORK_PAGE_CLOSE_TIMEOUT_MS", "0").strip()
    try:
        v = float(raw)
        return max(0.0, v)
    except ValueError:
        return 0.0


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _auto_auth_config_enabled() -> bool:
    """Mặc định bật; UPWORK_AUTO_AUTH_CONFIG=0|false|no|off để không ghi .auth/auth_config.json."""
    raw = os.environ.get("UPWORK_AUTO_AUTH_CONFIG", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


async def _log_localstorage_oauth_keys(page) -> None:
    """In danh sách key localStorage có vẻ liên quan oauth/token (chỉ tên key)."""
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
        log(f"[DEBUG_TOKEN_MAP] localStorage keys (lọc oauth|token|auth|session|bearer): {keys}")
    except Exception as exc:
        log(f"[DEBUG_TOKEN_MAP] localStorage scan failed: {exc}")


async def _log_bearer_cookie_map(request, context) -> None:
    """So khớp giá trị Authorization Bearer với cookie trong context (chỉ log tên cookie)."""
    h = request.headers
    auth = h.get("authorization") or h.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        log("[DEBUG_TOKEN_MAP] không có Authorization: Bearer … trên request này")
        return
    token = auth[7:].strip()
    cookies = await context.cookies()
    exact = [c["name"] for c in cookies if (c.get("value") or "") == token]
    contains = [c["name"] for c in cookies if token and token in (c.get("value") or "")]
    if exact:
        log(f"[DEBUG_TOKEN_MAP] Bearer khớp **nguyên** giá trị các cookie: {exact}")
        log(
            "[DEBUG_TOKEN_MAP] → Gợi ý: đặt auth_config bearer_cookie hoặc .auth/bearer.txt "
            "theo một trong các tên trên nếu gọi API ngoài trình duyệt."
        )
        if _auto_auth_config_enabled():
            try:
                pick = preferred_graphql_bearer_cookie_name(exact)
                auth_dir = ROOT / ".auth"
                if merge_auth_config_bearer_cookie(pick, auth_dir):
                    log(
                        f"[AUTO_AUTH_CONFIG] đã cập nhật {auth_dir / 'auth_config.json'} "
                        f"— bearer_cookie={pick!r}"
                    )
                else:
                    log(
                        f"[AUTO_AUTH_CONFIG] {auth_dir / 'auth_config.json'} đã có bearer_cookie={pick!r} "
                        "(không ghi đè)"
                    )
            except Exception as exc:
                log(f"[AUTO_AUTH_CONFIG] không ghi auth_config: {exc!r}")
    elif contains:
        log(f"[DEBUG_TOKEN_MAP] Bearer là **phần** của giá trị cookie (substring): {contains}")
    else:
        log(
            "[DEBUG_TOKEN_MAP] Bearer **không** trùng cookie HttpOnly nào trong storage_state — "
            "thường do runtime JS ghép header (hoặc token chỉ trong memory). "
            "Vẫn có thể copy Bearer từ request này (DEBUG_LOG_SENSITIVE=1) hoặc Network tab."
        )


async def run() -> None:
    global _log_sink, _log_path

    brave = _default_brave_executable()
    if not brave:
        print(
            "Không tìm thấy Brave. Cài Brave hoặc set BRAVE_EXECUTABLE=/path/to/Brave Browser",
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
                        f"no storage_state at {storage_path} — đăng nhập tay; "
                        "phiên sẽ được lưu khi thoát hoặc theo chu kỳ."
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
                log("Vào Job search để trigger userJobSearch; JSON response trong captures/")
                if debug_token_map:
                    log(
                        "DEBUG_TOKEN_MAP=1 — sẽ log tên cookie khớp Bearer (không in token). "
                        "Script không sinh token; token do trang Upwork đặt cookie/JS."
                    )
                    if _auto_auth_config_enabled():
                        log(
                            "UPWORK_AUTO_AUTH_CONFIG (mặc định bật): khi có exact Bearer↔cookie, "
                            "ghi bearer_cookie vào .auth/auth_config.json (tắt: UPWORK_AUTO_AUTH_CONFIG=0)."
                        )
                log("Thoát: đóng tab/cửa sổ hoặc Ctrl+C (phiên vẫn được lưu trong finally).\n")

                await page.goto(start_url, wait_until="domcontentloaded")
                log(f"goto done: {page.url}")

                close_ms = int(_page_close_timeout_ms())
                if close_ms > 0:
                    log(f"waiting for page close (timeout {close_ms} ms)")
                    await page.wait_for_event("close", timeout=close_ms)
                else:
                    log("waiting for page close (no timeout — đóng tab khi xong)")
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
