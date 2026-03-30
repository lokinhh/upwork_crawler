# Debug Upwork GraphQL (`userJobSearch`)

Công cụ chạy **Brave** có giao diện, đăng nhập tay, rồi **bắt request/response** POST tới:

`https://www.upwork.com/api/graphql/v1?alias=userJobSearch`

Dùng để điều tra payload GraphQL (query/variables) và JSON trả về — không thay thế API chính thức của Upwork.

## Thư mục `.auth/` (Playwright)

Sau khi chạy `capture_user_job_search.py`, file **`.auth/storage_state.json`** chứa cookie + `localStorage`. Các script sau **đọc tự động** từ đó (không cần `export` tay), trừ khi bạn đã set biến môi trường — **env luôn ghi đè file**:

| Nguồn | Ý nghĩa |
|--------|--------|
| Cookie header | Ghép từ mảng `cookies` trong `storage_state.json` |
| `Authorization: Bearer …` | Suy ra từ cookie: **ưu tiên cookie tên `*sb`** (client GraphQL/search), trước `oauth2_global_js_token` (scope khác, dễ lỗi quyền field) |
| `x-upwork-api-tenantid` | Cookie `current_organization_uid` |
| `FLARESOLVERR_URL` / warm URL | Mặc định `http://localhost:8191` và URL job search; có thể sửa trong **`.auth/auth_config.json`** (xem `auth_config.json.example`) |

Tùy chọn copy `auth_config.json.example` → `.auth/auth_config.json` và chỉnh `bearer_cookie`, `tenant_id`, `flaresolverr_url`, …

**Bearer khớp DevTools (khi log đã redact token):** tạo file **`.auth/bearer.txt`** — một dòng, nội dung copy từ Network → request `userJobSearch` → header **Authorization** (hoặc chỉ phần `oauth2v2_int_…`). File này ghi đè token suy ra từ cookie (trừ khi đã `export UPWORK_AUTHORIZATION=...`).

**Phân tích log cũ:** `python analyze_capture_log.py captures/debug_session_*.log` — báo rõ nếu `authorization` là `<redacted>` (không thể “đọc” token từ log).

**In shell để debug:** `eval "$(python3 export_auth_env.py)"`

## Postman

1. **Import** file `Upwork_userJobSearch.postman_collection.json` (Postman → Import → chọn file).
2. Mở collection → tab **Variables** (hoặc tạo Environment từ `postman_environment.example.json`) và điền:
   - **`authorization`**: nguyên giá trị header `Authorization` từ DevTools (vd. `Bearer oauth2v2_int_...`).
   - **`cookie`**: nguyên chuỗi header `Cookie` từ cùng request `userJobSearch`.
   - **`tenant_id`**: giá trị `x-upwork-api-tenantid`.
   - **`referer`**: nên trùng URL search bạn đang dùng (ảnh hưởng một số kiểm tra phía server).
3. Trong request **Body** (raw JSON) sửa `variables.requestVariables` (`userQuery`, `sort`, `paging.offset` / `count`) nếu cần.
4. **Send**. Nếu `401` / `403` / challenge: token hoặc cookie hết hạn — copy lại từ trình duyệt sau khi đăng nhập; đôi khi cần thêm header trace (`vnd-eo-*`) giống hệt bản copy từ Network.

File `postman_userJobSearch_body.json` là bản tách của body (cùng nội dung với collection) để chỉnh ngoài Postman rồi dán lại nếu muốn.

### curl

Nếu đã có **`.auth/storage_state.json`**, chỉ cần:

```bash
chmod +x curl_userJobSearch.sh
./curl_userJobSearch.sh
```

Hoặc dùng `curl.env` / export tay — **env ghi đè** giá trị suy ra từ `.auth`. Body: `postman_userJobSearch_body.json`.

- `UPWORK_OUT=./resp.json` — ghi body ra file, headers response in ra stderr.
- `UPWORK_VERBOSE=1` — bật `curl -v`.

### 403 dù đã có Authorization / Cookie — và FlareSolverr

**Có auth vẫn có thể 403** vì:

- Cloudflare lọc theo **TLS / fingerprint**; `curl` không giống Chrome.
- Thiếu hoặc hết hạn **`cf_clearance`**, **`__cf_bm`** (cookie CF), hoặc không khớp **User-Agent** với phiên đã qua challenge.

**FlareSolverr** (`http://localhost:8191`) mở trang bằng trình duyệt headless → lấy cookie CF mới. Nó **không** thay thế cookie đăng nhập Upwork; cần **gộp** cookie từ FlareSolverr với cookie bạn copy từ DevTools (session).

Chạy (Docker compose đã có FlareSolverr → port **8191**):

```bash
docker compose up -d flaresolverr   # nếu chưa chạy
cd debug_upwork_graphql
pip install -r requirements.txt
python graphql_via_flaresolverr.py
```

Đủ **`.auth/storage_state.json`** là đủ; chỉ cần `export …` nếu muốn ghi đè (hoặc thêm `.auth/auth_config.json` cho URL FlareSolverr / warm URL).

Script: GET qua FlareSolverr tới `UPWORK_WARM_URL` (mặc định trang job search) → merge cookies → POST GraphQL. Nếu vẫn 403, thử copy lại token/cookie từ trình duyệt vừa mở hoặc tăng `FLARESOLVERR_TIMEOUT_MS`.

**Lưu ý:** Gọi GraphQL “như API” có thể trái ToS; chỉ dùng để debug cá nhân.

## Yêu cầu

- **Python** 3.9+ (khuyên 3.11)
- **Trình duyệt Brave** đã cài (macOS: thường ở `/Applications/Brave Browser.app/...`)

## Cài đặt và chạy

Từ thư mục gốc repo:

```bash
cd debug_upwork_graphql
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python capture_user_job_search.py
```

1. Mở cửa sổ Brave → đăng nhập Upwork (lần đầu).
2. Vào **Job search**, chỉnh filter / lật trang để site gọi lại `userJobSearch`.
3. Trong terminal xem tóm tắt; **chi tiết đầy đủ** nằm trong file log và thư mục `captures/`.
4. Thoát: đóng tab/cửa sổ hoặc **Ctrl+C**. Phiên đăng nhập được lưu để lần sau bớt nhập lại.

Script **chờ đóng tab không giới hạn thời gian** (mặc định). Trước đây Playwright chờ `close` với timeout 30s nên dễ lỗi nếu bạn vẫn đang lướt. Muốn giới hạn (vd. test tự động): `UPWORK_PAGE_CLOSE_TIMEOUT_MS=120000`.

## File Postman / body (commit được, không chứa secret)

| File | Mô tả |
|------|--------|
| `Upwork_userJobSearch.postman_collection.json` | Collection v2.1 — request `userJobSearch` + biến `authorization` / `cookie` / … |
| `postman_userJobSearch_body.json` | Body GraphQL (`query` + `variables`) trích từ capture |
| `postman_environment.example.json` | Mẫu environment — **copy ra file riêng**, điền secret local, không commit |
| `curl_userJobSearch.sh` | Script `curl` — đọc `postman_userJobSearch_body.json` + biến `UPWORK_*` |
| `curl.env.example` | Mẫu cho `curl.env` (copy → điền; `curl.env` đã gitignore) |
| `graphql_via_flaresolverr.py` | Đọc `.auth/storage_state.json`, merge CF (FlareSolverr), **tính lại Bearer từ cookie đã merge**, POST GraphQL |
| `auth_loader.py` | Đọc `.auth/storage_state.json` (+ `auth_config.json`) → Bearer, Cookie, tenant, URL |
| `export_auth_env.py` | In `export VAR=...` cho shell (`eval "$(python3 export_auth_env.py)"`) |
| `auth_config.json.example` | Mẫu copy → `.auth/auth_config.json` |
| `analyze_capture_log.py` | Kiểm tra log capture có redact Authorization hay không |
| `.auth/bearer.txt` | (tuỳ chọn) một dòng Bearer copy từ DevTools — ưu tiên hơn suy từ cookie |

## File sinh ra (đã `.gitignore`)

| Đường dẫn | Mô tả |
|-----------|--------|
| `.auth/storage_state.json` | Cookie + `localStorage` (Playwright `storage_state`) |
| `captures/debug_session_*.log` | Log chi tiết phiên (mặc định mỗi lần chạy một file) |
| `captures/userJobSearch_response_*.json` | Body response GraphQL đầy đủ |
| `captures/userJobSearch_request_*.json` | Chỉ khi bật `SAVE_REQUEST_JSON=1` |

**Không commit** `.auth/`, `captures/`, và không đẩy log chứa cookie/token lên git/public.

## Biến môi trường

| Biến | Ý nghĩa |
|------|--------|
| `BRAVE_EXECUTABLE` | Đường dẫn binary Brave nếu không tự tìm thấy |
| `UPWORK_START_URL` | URL mở khi start (mặc định trang login Upwork) |
| `UPWORK_STORAGE_STATE` | File lưu phiên (mặc định `.auth/storage_state.json`) |
| `UPWORK_DEBUG_LOG` | File log: đường dẫn tùy chỉnh; để trống = tự tạo trong `captures/`; `0` / `off` = không ghi file |
| `SAVE_REQUEST_JSON` | `1` = ghi thêm file JSON request vào `captures/` |
| `STORAGE_SAVE_INTERVAL` | Giây — lưu phiên định kỳ (mặc định `120`, `0` = tắt) |
| `DEBUG_UPWORK_CONSOLE` | `1` = in `console.log` từ trang lên terminal/log |
| `DEBUG_LOG_SENSITIVE` | `1` = log nguyên header `cookie` / `authorization` (**rất rủi ro**, chỉ local) |
| `DEBUG_TOKEN_MAP` | `1` = khi có `userJobSearch`, log **tên cookie** nào trùng giá trị Bearer (không in token) + key `localStorage` gợi ý — để hiểu token đến từ đâu |
| `UPWORK_PAGE_CLOSE_TIMEOUT_MS` | Thời gian chờ đóng tab (ms). `0` (mặc định) = chờ vô hạn; tránh timeout 30s mặc định của Playwright |

Ví dụ một lần chạy với log cố định:

```bash
export UPWORK_DEBUG_LOG="$PWD/captures/my_run.log"
python capture_user_job_search.py
```

## Gợi ý xử lý sự cố

- **Không thấy Brave**: cài Brave hoặc set `BRAVE_EXECUTABLE`.
- **Vẫn bắt buộc đăng nhập mỗi lần**: xóa `.auth/storage_state.json` thử lại từ đầu; kiểm tra Upwork có hết hạn phiên không.
- **Không có request `userJobSearch`**: mở đúng trang search job và đợi/đổi filter để danh sách job load lại.
- **HTTP 200 nhưng GraphQL**: `Requested oAuth2 client does not have permission to see some of the requested fields` — **không phải** “sai mật khẩu”: token đã được chấp nhận nhưng **một số field trong câu query** (vd. `upworkHistoryData`, `totalSpent`, facets…) **không thuộc scope** OAuth client gắn với token khi gọi kiểu này. **Cách xử lý:** chạy `UPWORK_GRAPHQL_MINIMAL=1 python graphql_via_flaresolverr.py` (body tối giản trong `postman_userJobSearch_body.minimal.json`); hoặc copy **nguyên** `query` + `variables` từ request `userJobSearch` **200** trong DevTools vào file body tùy chỉnh rồi `UPWORK_GRAPHQL_BODY=.../my.json`. Có thể kèm **`.auth/bearer.txt`** nếu token suy từ cookie vẫn lệch.

## Lưu ý

Tự động hóa / thu thập dữ liệu có thể **không phù hợp Điều khoản** của Upwork. Chỉ dùng công cụ này cho mục đích cá nhân, hiểu rủi ro với tài khoản.
