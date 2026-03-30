# Upwork Scanner → Telegram

Ứng dụng Python chạy vòng lặp: lấy job từ **Upwork qua GraphQL** (`userJobSearch`), lọc job mới, **tóm tắt bằng LLM**, rồi gửi tin qua **Telegram bot** cho mọi chat đã `/start`.

Luồng tóm tắt: **9Router** (nếu có `NINEROUTER_MODEL`) → **OpenRouter** → **Gemini** — xem `upwork/clients/summarizer.py`.

## Cách hoạt động (thực tế trong code)

1. **Lấy job**: chỉ **GraphQL** + **FlareSolverr** (vượt Cloudflare) + thư mục **`.auth/`** (cookie / Playwright `storage_state.json`). Không còn scrape HTML trong scanner; `upwork/fetchers/scrape.py` là phần cũ, scanner không gọi.
2. **Trùng lặp**: `SeenStore` (mặc định `.seen_jobs.json`).
3. **Telegram**: đồng bộ subscriber qua `getUpdates`; `TELEGRAM_CHAT_ID` để trống / `*` / `all` = gửi cho tất cả đã `/start`.
4. **Ghi log**: mặc định `logs/upwork_scanner.log` (có thể đổi bằng `UPWORK_LOG_DIR`, `UPWORK_LOG_FILE`, `UPWORK_LOG_LEVEL`).

> **`UPWORK_FEED_URL` (RSS)**: vẫn có trong `Config` để thỏa “ít nhất một nguồn” khi validate env, nhưng **vòng quét hiện không đọc RSS** — cần **`UPWORK_SEARCH_KEYWORD`** và **`FLARESOLVERR_URL`** thì mới fetch được job.

Chi tiết module và sơ đồ: **`ARCHITECTURE.md`**.

## Yêu cầu

- Python 3.11+ (khuyến nghị; image Docker dùng 3.11).
- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) đang chạy (local hoặc Docker) nếu dùng tìm kiếm theo keyword.
- Playwright Chromium (login Upwork): `pip install playwright` rồi `playwright install chromium`.

## Cài đặt

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Cấu hình

```bash
cp .env.example .env
```

Biến bắt buộc / cần cho chạy thực tế:

| Nhóm | Biến | Ghi chú |
|------|------|---------|
| Job | `UPWORK_SEARCH_KEYWORD` | Từ khóa hoặc nhiều mục cách nhau bằng dấu phẩy; có thể là URL trang search Upwork — xử lý trong `fetchers/keyword.py`. |
| Job | `FLARESOLVERR_URL` | Ví dụ `http://localhost:8191`. Trong Docker Compose crawler đã override thành `http://flaresolverr:8191`. |
| Telegram | `TELEGRAM_BOT_TOKEN` | Token từ BotFather. |
| Tóm tắt | Một trong các cách | `NINEROUTER_MODEL` (+ tùy chọn `NINEROUTER_BASE_URL`, `NINEROUTER_API_KEY`), hoặc `OPENROUTER_API_KEY` + `OPENROUTER_MODEL`, hoặc `GEMINI_API_KEY` / file `api_key_gemini.txt` (mỗi dòng một key). |

Đăng nhập Upwork (lần đầu hoặc khi hết phiên):

- `UPWORK_EMAIL` / `UPWORK_PASSWORD`, và tùy chọn `UPWORK_AUTO_LOGIN`, `UPWORK_LOGIN_FORM`, `UPWORK_AUTH_DIR` (mặc định `./.auth`).
- Có thể chạy thủ công: `python -m upwork.tools.login_via_flaresolverr` (xem comment trong `.env.example`).

Biến tùy chọn khác (một phần): `POLL_INTERVAL_SECONDS` (trong `.env.example` mặc định 300), `SEEN_STORE_PATH`, `TELEGRAM_SUBSCRIBERS_STORE_PATH`, `UPWORK_GRAPHQL_SORT`, `UPWORK_GRAPHQL_PAGE_SIZE`, `UPWORK_GRAPHQL_403_MAX_RETRIES`, `FLARESOLVERR_TIMEOUT_MS`, `GEMINI_MODEL`, v.v. — đầy đủ trong **`.env.example`**.

## Chạy local

Bật FlareSolverr, cấu hình `.env`, rồi:

```bash
python upwork_scanner.py
```

hoặc:

```bash
python -m upwork.main
```

(Lệnh phải chạy từ **thư mục gốc repo** để import package `upwork`.)

## Chạy bằng Docker Compose

Stack: **FlareSolverr** + **9Router** (build từ `./9router`) + **crawler** (`Dockerfile.crawler`).

```bash
cp .env.example .env
# Điền TELEGRAM_BOT_TOKEN, UPWORK_SEARCH_KEYWORD, và backend tóm tắt (ví dụ NINEROUTER_MODEL trong .env)
docker compose up -d --build
```

- FlareSolverr: `http://localhost:8191`
- 9Router: `http://localhost:20128` — cấu hình thêm trong `9router/.env` nếu cần (xem `docker-compose.yml` và ghi chú trong repo 9Router).
- Volume: `crawler_data` (seen + subscribers), `crawler_auth` (`.auth` trong container), mount `./logs` → `/app/logs`.

## Cấu trúc thư mục (gỡ lỗi)

```
.
├── upwork_scanner.py          # Entry: gọi upwork.main.main()
├── requirements.txt
├── docker-compose.yml
├── Dockerfile.crawler
├── .env.example
├── ARCHITECTURE.md
└── upwork/
    ├── main.py                # Nối Config, stores, clients, chạy UpworkScanner
    ├── config.py
    ├── scanner.py             # Vòng lặp: sync Telegram → fetch → tóm tắt → gửi
    ├── session/ensure.py     # Chuẩn bị phiên GraphQL / login
    ├── auth/                   # Đọc storage_state, Bearer, cookie
    ├── fetchers/
    │   ├── jobs.py            # Gọi GraphQL theo keyword
    │   ├── graphql_search.py  # POST userJobSearch + FlareSolverr
    │   └── keyword.py
    ├── clients/
    │   ├── summarizer.py      # 9Router → OpenRouter → Gemini
    │   ├── ninerouter.py
    │   ├── openrouter.py
    │   ├── gemini.py
    │   └── telegram.py
    ├── stores/                # seen + subscribers
    └── tools/
        └── login_via_flaresolverr.py
```

## Lưu ý

- Upwork **không còn RSS chính thức** như trước; nguồn ổn định trong project này là **GraphQL + FlareSolverr + `.auth`**.
- File **`.seen_jobs.json`** (hoặc đường dẫn bạn đặt) ghi nhớ job đã gửi; **`.telegram_subscribers.json`** lưu subscriber và `last_update_id`.
- Khi đổi cách tóm tắt, sửa prompt trong từng client (`gemini`, `openrouter`, `ninerouter`) hoặc luồng trong `SummarizerClient`.
