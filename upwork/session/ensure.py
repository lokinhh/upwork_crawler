"""Chạy login Playwright (tuỳ chọn) để tạo `.auth/storage_state.json`."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from ..config import Config

LOGGER = logging.getLogger("upwork.session")


def run_login_subprocess(config: Config) -> None:
    """Gọi `python -m upwork.tools.login_via_flaresolverr` (FlareSolverr + Playwright, lưu `.auth`)."""
    env = os.environ.copy()
    env["UPWORK_EMAIL"] = config.upwork_email
    env["UPWORK_PASSWORD"] = config.upwork_password
    env["UPWORK_AUTH_DIR"] = str(config.upwork_auth_dir.resolve())
    if config.flaresolverr_url:
        env["FLARESOLVERR_URL"] = config.flaresolverr_url
    # Giống hướng debug: form login — iovation/request thật từ trình duyệt
    env["UPWORK_LOGIN_FORM"] = "1" if config.upwork_login_form else "0"

    # Ghi log login chi tiết ra thư mục log để persist trên host khi có volume mount.
    log_dir = Path((env.get("UPWORK_LOG_DIR") or env.get("LOG_DIR") or "/app/logs")).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    env.setdefault("UPWORK_LOGIN_DEBUG", "1")
    env["UPWORK_LOGIN_DEBUG_LOG"] = str(log_dir / f"login_debug_{stamp}.log")
    subprocess.run(
        [sys.executable, "-m", "upwork.tools.login_via_flaresolverr"],
        check=True,
        env=env,
    )


def ensure_graphql_session(config: Config) -> None:
    """
    Chưa có `storage_state.json` và có UPWORK_EMAIL + UPWORK_PASSWORD → chạy login subprocess.
    (Không cần UPWORK_AUTO_LOGIN=1 — có credential là đủ để tạo .auth lần đầu.)
    """
    storage = config.upwork_auth_dir / "storage_state.json"
    if storage.is_file():
        return

    if not config.upwork_email or not config.upwork_password:
        raise FileNotFoundError(
            f"Thiếu phiên GraphQL: {storage}. Đặt UPWORK_EMAIL và UPWORK_PASSWORD trong .env, "
            "hoặc chạy một lần: python -m upwork.tools.login_via_flaresolverr"
        )

    LOGGER.info("Chưa có storage_state — chạy đăng nhập (Playwright + FlareSolverr)…")
    run_login_subprocess(config)
    if not storage.is_file():
        raise RuntimeError(f"Đăng nhập xong nhưng không thấy {storage}")
