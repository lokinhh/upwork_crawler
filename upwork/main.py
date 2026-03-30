"""
Wire dependencies and run the scanner.
Có thể gọi: python -m upwork.main (chạy từ thư mục gốc repo).
"""
import logging
import os
import warnings
from pathlib import Path
from typing import List, Optional

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

from .config import Config
from .stores import SeenStore, TelegramSubscribersStore
from .clients import (
    GeminiClient,
    NineRouterClient,
    OpenRouterClient,
    SummarizerClient,
    TelegramClient,
)
from .scanner import UpworkScanner


def _load_gemini_keys(config: Config) -> List[str]:
    """
    Load Gemini API keys, ưu tiên danh sách trong api_key_gemini.txt (mỗi dòng 1 key).
    Nếu vẫn không có, fallback sang GEMINI_API_KEY đơn lẻ trong config.
    """
    keys: List[str] = []

    # File nằm ở thư mục gốc repo (cùng cấp với README.md, .env, api_key_gemini.txt)
    project_root = Path(__file__).resolve().parent.parent
    keys_file = project_root / "api_key_gemini.txt"
    if keys_file.exists():
        for line in keys_file.read_text(encoding="utf-8").splitlines():
            k = line.strip()
            if k:
                keys.append(k)

    # Thêm key đơn lẻ trong config nếu có, ưu tiên đứng đầu
    if config.gemini_api_key:
        if config.gemini_api_key not in keys:
            keys.insert(0, config.gemini_api_key)

    return keys


def _load_ninerouter_client() -> Optional[NineRouterClient]:
    """
    Tạo NineRouterClient nếu có NINEROUTER_API_KEY + NINEROUTER_MODEL.
    Key mặc định local thường là sk_9router (có thể set trong .env).
    """
    import os

    api_key = os.getenv("NINEROUTER_API_KEY", "sk_9router").strip() or "sk_9router"
    model = os.getenv("NINEROUTER_MODEL", "").strip()

    if not model:
        return None

    try:
        return NineRouterClient(api_key=api_key, model=model)
    except ValueError as exc:
        logging.getLogger("upwork.main").error("NineRouterClient init failed: %s", exc)
        return None


def _load_openrouter_client() -> Optional[OpenRouterClient]:
    """
    Tạo OpenRouterClient nếu có cấu hình OPENROUTER_API_KEY + OPENROUTER_MODEL.
    Nếu thiếu một trong hai thì trả về None (không dùng OpenRouter).
    """
    import os

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", "").strip()

    if not api_key or not model:
        return None

    try:
        return OpenRouterClient(api_key=api_key, model=model)
    except ValueError as exc:
        logging.getLogger("upwork.main").error("OpenRouterClient init failed: %s", exc)
        return None



def _setup_logging() -> Path:
    """Log ra console + file (mặc định logs/upwork_scanner.log)."""
    level_name = (os.getenv("UPWORK_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    default_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir = Path((os.getenv("UPWORK_LOG_DIR") or os.getenv("LOG_DIR") or str(default_dir)).strip()).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / (os.getenv("UPWORK_LOG_FILE") or "upwork_scanner.log")

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    sh = logging.StreamHandler()
    sh.setLevel(level)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("upwork.main").info("Logging to file: %s", log_file)
    return log_dir

def main() -> None:
    log_dir = _setup_logging()
    config = Config.from_env()
    os.environ.setdefault("UPWORK_LOG_DIR", str(log_dir))
    seen_store = SeenStore(config.seen_store_path)
    subscribers_store = TelegramSubscribersStore(config.telegram_subscribers_store_path)

    gemini_keys = _load_gemini_keys(config)
    gemini_client: Optional[GeminiClient] = None
    if gemini_keys:
        gemini_client = GeminiClient(api_keys=gemini_keys, model=config.gemini_model)

    ninerouter_client = _load_ninerouter_client()
    openrouter_client = _load_openrouter_client()

    summarizer = SummarizerClient(
        gemini=gemini_client,
        openrouter=openrouter_client,
        ninerouter=ninerouter_client,
    )

    telegram = TelegramClient(
        bot_token=config.telegram_bot_token,
        default_chat_id=config.telegram_chat_id,
    )
    scanner = UpworkScanner(
        config,
        seen_store,
        subscribers_store,
        summarizer,
        telegram,
    )
    scanner.run_forever()


if __name__ == "__main__":
    main()
