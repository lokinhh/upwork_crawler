# Debug Upwork GraphQL (`userJobSearch`)

The **Brave** running tool has an interface, manually log in, then **capture request/response** POST to:

`https://www.upwork.com/api/graphql/v1?alias=userJobSearch`

Used to investigate GraphQL payload (query/variables) and returned JSON — does not replace Upwork's official API.

## `.auth/` directory (Playwright)

After running `capture_user_job_search.py`, the file **`.auth/storage_state.json`** contains cookies + `localStorage`. The following scripts **read automatically** from there (no manual `export` required), unless you have set an environment variable — **env always overwrites file**:

| Source | Meaning |
|--------|--------|
| Cookie header | Concatenate from `cookies` array in `storage_state.json` |
| `Authorization: Bearer …` | Inferred from cookies: **prefer cookie named `*sb`** (GraphQL/search client), before `oauth2_global_js_token` (different scope, prone to field permission errors) |
| `x-upwork-api-tenantid` | Cookie `current_organization_uid` |
| `FLARESOLVERR_URL` /warm URL | Default `http://localhost:8191` and URL job search; editable in **`.auth/auth_config.json`** (see `auth_config.json.example`) |

Optionally copy `auth_config.json.example` → `.auth/auth_config.json` and adjust `bearer_cookie`, `tenant_id`, `flaresolverr_url`, …

**Bearer matches DevTools (when log has redacted token):** creates file **`.auth/bearer.txt`** — one line, content copied from Network → request `userJobSearch` → **Authorization** header (or just the `oauth2v2_int_…` part). This file overrides the token derived from the cookie (unless `export UPWORK_AUTHORIZATION=...` has been given).

**Analyze old log:** `python analyze_capture_log.py captures/debug_session_*.log` — specify if `authorization` is `<redacted>` (cannot “read” tokens from log).

**Print shell for debugging:** `eval "$(python3 export_auth_env.py)"`

## Postman

1. **Import** file `Upwork_userJobSearch.postman_collection.json` (Postman → Import → select file).
2. Open collection → **Variables** tab (or create Environment from `postman_environment.example.json`) and fill in:
   - **`authorization`**: original `Authorization` header value from DevTools (e.g. `Bearer oauth2v2_int_...`).
   - **`cookie`**: original `Cookie` header string from the same `userJobSearch` request.
   - **`tenant_id`**: value `x-upwork-api-tenant`.
   - **`referer`**: should match the search URL you are using (affects some server-side checks).
3. In the request **Body** (raw JSON) edit `variables.requestVariables` (`userQuery`, `sort`, `paging.offset` / `count`) if necessary.
4. **Send**. If `401` / `403` / challenge: token or cookie expired — copy from browser after login; Sometimes it is necessary to add a header trace (`vnd-eo-*`) exactly like the copy from Network.

The file `postman_userJobSearch_body.json` is a split copy of the body (same content as the collection) that can be edited outside of Postman and then pasted if desired.

### curl

If **`.auth/storage_state.json`** already exists, just:

```bash
chmod +x curl_userJobSearch.sh
./curl_userJobSearch.sh
```

Or use `curl.env` / export manually — **env overrides** the value derived from `.auth`. Body: `postman_userJobSearch_body.json`.

- `UPWORK_OUT=./resp.json` — ghi body ra file, headers response in ra stderr.
- `UPWORK_VERBOSE=1` — enable `curl -v`.

### 403 despite Authorization / Cookies — and FlareSolverr

**With auth it can still 403** because:

- Cloudflare filters by **TLS / fingerprint**; `curl` is not like Chrome.
- Missing or expired **`cf_clearance`**, **`__cf_bm`** (CF cookie), or mismatched **User-Agent** with the challenged session.

**FlareSolverr** (`http://localhost:8191`) opens the page in a headless browser → gets the new CF cookie. It **doesn't** replace the Upwork login cookie; need to **merge** the cookie from FlareSolverr with the cookie you copied from DevTools (session).

Run (Docker compose already has FlareSolverr → port **8191**):

```bash
docker compose up -d flaresolverr # if not already running
cd debug_upwork_graphql
pip install -r requirements.txt
python graphql_via_flaresolverr.py
```

**`.auth/storage_state.json`** is enough; just `export …` if you want to override (or add `.auth/auth_config.json` for FlareSolverr / warm URL).

Script: GET via FlareSolverr to `UPWORK_WARM_URL` (default job search page) → merge cookies → POST GraphQL. If still 403, try copying the token/cookie from the browser you just opened or increasing `FLARESOLVERR_TIMEOUT_MS`.

**Note:** Calling GraphQL “as an API” may violate the ToS; For personal debugging purposes only.

## Request

- **Python** 3.9+ (3.11 recommended)
- **Brave browser** installed (macOS: usually located at `/Applications/Brave Browser.app/...`)

## Install and run

From the root repo directory:

```bash
cd debug_upwork_graphql
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python capture_user_job_search.py
```

1. Open the Brave window → log in to Upwork (first time).
2. Go to **Job search**, adjust the filter / turn the page so that the site calls back `userJobSearch`.
3. In terminal view summary; **full details** are in the log file and `captures/` directory.
4. Exit: close tab/window or **Ctrl+C**. The login session is saved to reduce re-entry next time.

Script **wait for tab close indefinitely** (default). Previously, Playwright waited for `close` with a timeout of 30 seconds, so it was easy to fail if you were still surfing. Want to limit (eg. automated testing): `UPWORK_PAGE_CLOSE_TIMEOUT_MS=120000`.

## File Postman / body (can be committed, does not contain secrets)

| File | Description |
|------|--------|
| `Upwork_userJobSearch.postman_collection.json` | Collection v2.1 — request `userJobSearch` + variable `authorization` / `cookie` / … |
| `postman_userJobSearch_body.json` | Body GraphQL (`query` + `variables`) extracted from capture |
| `postman_environment.example.json` | Environment template — **copy to separate file**, fill in local secret, do not commit |
| `curl_userJobSearch.sh` | Script `curl` — read `postman_userJobSearch_body.json` + variable `UPWORK_*` |
| `curl.env.example` | Template for `curl.env` (copy → fill; `curl.env` gitignore) |
| `graphql_via_flaresolverr.py` | Read `.auth/storage_state.json`, merge CF (FlareSolverr), **recalculate Bearer from merged cookie**, POST GraphQL |
| `auth_loader.py` | Read `.auth/storage_state.json` (+ `auth_config.json`) → Bearer, Cookie, tenant, URL |
| `export_auth_env.py` | In `export VAR=...` cho shell (`eval "$(python3 export_auth_env.py)"`) |
| `auth_config.json.example` | Sample copy → `.auth/auth_config.json` |
| `analyze_capture_log.py` | Check whether log capture has redact Authorization |
| `.auth/bearer.txt` | (optional) a Bearer line copied from DevTools — preferred over cookie inference |

## Generated file (with `.gitignore`)

| Path | Description |
|-----------|--------|
| `.auth/storage_state.json` | Cookie + `localStorage` (Playwright `storage_state`) |
| `captures/debug_session_*.log` | Log session details (default one file per run) |
| `captures/userJobSearch_response_*.json` | Body response full GraphQL |
| `captures/userJobSearch_request_*.json` | Only when `SAVE_REQUEST_JSON=1` | is enabled

**Do not commit** `.auth/`, `captures/`, and do not push logs containing cookies/tokens to git/public.

## Environment variables

| Variable | Meaning |
|------|--------|
| `BRAVE_EXECUTABLE` | Brave binary path if not found |
| `UPWORK_START_URL` | URL to open on start (default Upwork login page) |
| `UPWORK_STORAGE_STATE` | Session storage file (default `.auth/storage_state.json`) |
| `UPWORK_DEBUG_LOG` | Log file: custom path; blank = self-generated in `captures/`; `0` / `off` = do not write file |
| `SAVE_REQUEST_JSON` | `1` = add JSON request file to `captures/` |
| `STORAGE_SAVE_INTERVAL` | Seconds — save sessions periodically (default `120`, `0` = off) |
| `DEBUG_UPWORK_CONSOLE` | `1` = print `console.log` from the page to terminal/log |
| `DEBUG_LOG_SENSITIVE` | `1` = log raw header `cookie` / `authorization` (**very risky**, local only) |
| `DEBUG_TOKEN_MAP` | `1` = when there is `userJobSearch`, log which **cookie name** matches the Bearer value (don't print the token) + key `localStorage` hint — to understand where the token came from |
| `UPWORK_PAGE_CLOSE_TIMEOUT_MS` | Tab closing timeout (ms). `0` (default) = wait indefinitely; avoid Playwright's default 30s timeout |

Example run with fixed log:

```bash
export UPWORK_DEBUG_LOG="$PWD/captures/my_run.log"
python capture_user_job_search.py
```

## Troubleshooting suggestions

- **Not seeing Brave**: install Brave or set `BRAVE_EXECUTABLE`.
- **Still required to log in every time**: delete `.auth/storage_state.json` try again from scratch; check if Upwork session expires.
- **No `userJobSearch` request**: open the correct job search page and wait/change the filter for the job list to reload.
- **HTTP 200 but GraphQL**: `Requested oAuth2 client does not have permission to see some of the requested fields` — **not** “wrong password”: token was accepted but **some fields in the query** (eg `upworkHistoryData`, `totalSpent`, facets…) **are not in scope** The OAuth client binds the token when calling this type. **Workaround:** run `UPWORK_GRAPHQL_MINIMAL=1 python graphql_via_flaresolverr.py` (minimal body in `postman_userJobSearch_body.minimal.json`); or copy **raw** `query` + `variables` from request `userJobSearch` **200** in DevTools into the custom body file and then `UPWORK_GRAPHQL_BODY=.../my.json`. **`.auth/bearer.txt`** can be included if the token derived from the cookie is still incorrect.

## Note

Automation/data collection may be **incompatible with Upwork's Terms**. Use this tool for personal use only, understanding the risks to your account.
