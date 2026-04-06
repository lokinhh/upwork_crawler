"""Run Playwright login (optional) to create `.auth/storage_state.json`."""
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
    """Call `python -m upwork.tools.login_via_flaresolverr` (FlareSolverr + Playwright, save `.auth`)."""
    env = os.environ.copy()
    env["UPWORK_EMAIL"] = config.upwork_email
    env["UPWORK_PASSWORD"] = config.upwork_password
    env["UPWORK_AUTH_DIR"] = str(config.upwork_auth_dir.resolve())
    if config.flaresolverr_url:
        env["FLARESOLVERR_URL"] = config.flaresolverr_url
    # Match debug path: form login with real iovation/request from browser.
    env["UPWORK_LOGIN_FORM"] = "1" if config.upwork_login_form else "0"

    # Write detailed login logs to log directory so they persist on host with volume mounts.
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
    If `storage_state.json` is missing and UPWORK_EMAIL + UPWORK_PASSWORD are available,
    run the login subprocess. (UPWORK_AUTO_LOGIN=1 is not required for first-time .auth creation.)
    """
    storage = config.upwork_auth_dir / "storage_state.json"
    if storage.is_file():
        return

    if not config.upwork_email or not config.upwork_password:
        raise FileNotFoundError(
            f"Missing GraphQL session: {storage}. Set UPWORK_EMAIL and UPWORK_PASSWORD in .env, "
            "or run once: python -m upwork.tools.login_via_flaresolverr"
        )

    LOGGER.info("storage_state is missing - running login (Playwright + FlareSolverr)...")
    run_login_subprocess(config)
    if not storage.is_file():
        raise RuntimeError(f"Login completed but {storage} was not created")
