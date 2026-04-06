# Upwork Scanner → Telegram

The Python application runs in a loop: retrieve jobs from **Upwork via GraphQL** (`userJobSearch`), filter new jobs, **summarize using LLM**, then send messages via **Telegram bot** to all chats that have `/started`.

Summary flow: **9Router** (if `NINEROUTER_MODEL` exists) → **OpenRouter** → **Gemini** — see `upwork/clients/summarizer.py`.

## How it works (actually in code)

1. **Get job**: just **GraphQL** + **FlareSolverr** (over Cloudflare) + **`.auth/`** folder (cookie / Playwright `storage_state.json`). No more scraping HTML in scanner; `upwork/fetchers/scrape.py` is old, scanner is not called.
2. **Duplicate**: `SeenStore` (default `.seen_jobs.json`).
3. **Telegram**: synchronize subscribers via `getUpdates`; `TELEGRAM_CHAT_ID` blank / `*` / `all` = send to all already `/start`.
4. **Logging**: default `logs/upwork_scanner.log` (can be changed with `UPWORK_LOG_DIR`, `UPWORK_LOG_FILE`, `UPWORK_LOG_LEVEL`).

> **`UPWORK_FEED_URL` (RSS)**: still present in `Config` to satisfy “at least one source” when validating env, but **scan loop does not currently read RSS** — needs **`UPWORK_SEARCH_KEYWORD`** and **`FLARESOLVERR_URL`** to fetch the job.

Module details and diagram: **`ARCHITECTURE.md`**.

## Request

- Python 3.11+ (recommended; Docker images use 3.11).
- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) is running (local or Docker) if using keyword search.
- Playwright Chromium (login Upwork): `pip install playwright` then `playwright install chromium`.

## Setting

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Configuration

```bash
cp .env.example .env
```

Required variables / needed for actual run:

| Group | Variable | Notes |
|------|------|---------|
| Job | `UPWORK_SEARCH_KEYWORD` | Keywords or multiple entries separated by commas; can be the Upwork search page URL — handled in `fetchers/keyword.py`. |
| Job | `FLARESOLVERR_URL` | For example `http://localhost:8191`. In Docker Compose crawler has overridden to `http://flaresolverr:8191`. |
| Telegram | `TELEGRAM_BOT_TOKEN` | Tokens from BotFather. |
| Summary | One of the ways | `NINEROUTER_MODEL` (+ options `NINEROUTER_BASE_URL`, `NINEROUTER_API_KEY`), or `OPENROUTER_API_KEY` + `OPENROUTER_MODEL`, or `GEMINI_API_KEY` / file `api_key_gemini.txt` (one key per line). |

Log in to Upwork (first time or when session ends):

- `UPWORK_EMAIL` / `UPWORK_PASSWORD`, and options `UPWORK_AUTO_LOGIN`, `UPWORK_LOGIN_FORM`, `UPWORK_AUTH_DIR` (default `./.auth`).
- Can be run manually: `python -m upwork.tools.login_via_flaresolverr` (see comment in `.env.example`).

Other (partial) optional variables: `POLL_INTERVAL_SECONDS` (in `.env.example` default 300), `SEEN_STORE_PATH`, `TELEGRAM_SUBSCRIBERS_STORE_PATH`, `UPWORK_GRAPHQL_SORT`, `UPWORK_GRAPHQL_PAGE_SIZE`, `UPWORK_GRAPHQL_403_MAX_RETRIES`, `FLARESOLVERR_TIMEOUT_MS`, `GEMINI_MODEL`, etc. — full in **`.env.example`**.

## Run locally

Enable FlareSolverr, configure `.env`, then:

```bash
python upwork_scanner.py
```

or:

```bash
python -m upwork.main
```

(Command must be run from **repo root directory** to import package `upwork`.)

## Runs using Docker Compose

Stack: **FlareSolverr** + **9Router** (image `ghcr.io/decolua/9router:latest`) + **crawler** (`Dockerfile.crawler`).

```bash
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, UPWORK_SEARCH_KEYWORD, and summary backend (e.g. NINEROUTER_MODEL in .env)
docker compose up -d --build
```

- FlareSolverr: `http://localhost:8191`
- 9Router: `http://localhost:20128` — service variable located in `docker-compose.yml` (`ninerouter.environment`); can be overridden via the original `.env` (prefix `NINEROUTER_*`, see `.env.example`).
- Volume: `crawler_data` (seen + subscribers), `crawler_auth` (`.auth` trong container), mount `./logs` → `/app/logs`.

## Directory structure (debug)

```
.
├── upwork_scanner.py # Entry: call upwork.main.main()
├── requirements.txt
├── docker-compose.yml
├── Dockerfile.crawler
├── .env.example
├── ARCHITECTURE.md
└── upwork/
    ├── main.py # Connect Config, stores, clients, run UpworkScanner
    ├── config.py
    ├── scanner.py # Loop: sync Telegram → fetch → summary → send
    ├── session/ensure.py # Prepare GraphQL session/login
    ├── auth/ # Read storage_state, Bearer, cookies
    ├── fetchers/
    │ ├── jobs.py # Call GraphQL by keyword
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

## Note

- Upwork **no longer has official RSS** like before; The stable source in this project is **GraphQL + FlareSolverr + `.auth`**.
- File **`.seen_jobs.json`** (or the path you set) remembers the submitted job; **`.telegram_subscribers.json`** stores subscribers and `last_update_id`.
- When changing the summary method, edit the prompt in each client (`gemini`, `openrouter`, `ninerouter`) or thread in `SummarizerClient`.
