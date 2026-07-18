# Kế hoạch hoàn chỉnh: ChatGPT Plus Web ↔ AutoCAD MCP qua HTTP

> **Trạng thái:** Đã hiệu chỉnh theo tài liệu OpenAI hiện hành và code thật của repo.
> **Ngày kiểm tra:** 2026-07-18
> **Repo:** `autocad-mcp`
> **Đường chính:** Streamable HTTP + ngrok hoặc Cloudflare Tunnel.
> **Ngoài phạm vi:** OpenAI Secure MCP Tunnel.

---

## 1. Kết luận khả thi

ChatGPT Developer mode hiện:

- Có trên tài khoản **Plus** ở bản web.
- Cho phép MCP tool **đọc và ghi**.
- Kết nối MCP từ xa bằng **SSE** hoặc **streaming HTTP**.
- Hỗ trợ **OAuth**, **No Authentication** và **Mixed Authentication**.
- Tôn trọng annotation `readOnlyHint`; tool không được đánh dấu chỉ đọc sẽ được coi là tool ghi và mặc định cần xác nhận.

Nguồn chính thức: <https://developers.openai.com/api/docs/guides/developer-mode>

Vì repo hiện chỉ chạy MCP qua `stdio`, việc bổ sung một entrypoint Streamable HTTP là khả thi và không cần viết lại backend AutoCAD.

---

## 2. Mục tiêu

Cho phép ChatGPT Plus web điều khiển AutoCAD LT trên máy Windows thông qua MCP server hiện có, với các yêu cầu:

1. Giữ `stdio` làm transport mặc định để không phá client cũ.
2. Bổ sung Streamable HTTP để ChatGPT web kết nối qua tunnel HTTPS.
3. Có hai mức vận hành tách biệt:
   - **Dev demo:** No Authentication, chỉ chạy ngắn hạn với tool/operation bị giới hạn mạnh.
   - **Production cá nhân:** OAuth, cho phép phạm vi đọc/ghi được kiểm soát.
4. Chặn các năng lực nguy hiểm khi kết nối từ xa, đặc biệt là `execute_lisp`.
5. Giới hạn file AutoCAD được phép mở, lưu và xuất.
6. Gắn annotation đúng để ChatGPT yêu cầu xác nhận cho thao tác ghi.
7. Có test, script PowerShell và tài liệu tiếng Việt đủ để dựng lại hệ thống.

### Kiến trúc mục tiêu

```text
[ChatGPT Plus web / Developer mode app]
                    |
                    | HTTPS MCP (Streamable HTTP)
                    | OAuth ở production
                    v
        [ngrok hoặc Cloudflare Tunnel]
                    |
                    | forward tới 127.0.0.1
                    v
     [autocad-mcp HTTP :8765/mcp]
                    |
                    | remote policy + audit + path guard
                    v
        [8 tools / remote-safe facade]
                    |
                    v
          [File IPC hoặc ezdxf]
                    |
                    v
    [AutoCAD LT + mcp_dispatch.lsp]
```

Tunnel chỉ chuyển tiếp vào loopback. MCP server không bind trực tiếp ra LAN hoặc Internet.

---

## 3. Hiện trạng repo và khoảng trống

| Thành phần | Hiện trạng |
|------------|------------|
| Entry point | `python -m autocad_mcp` gọi `server.main()` |
| MCP SDK trong lockfile | `mcp==1.26.0` |
| Transport | `mcp.run(transport="stdio")` |
| Tool | `drawing`, `entity`, `layer`, `block`, `annotation`, `pid`, `view`, `system` |
| Backend | `file_ipc` cho AutoCAD thật; `ezdxf` cho DXF headless |
| Đồng thời | File IPC đã có `asyncio.Lock`, mỗi lần chỉ chạy một command |
| Ảnh | Có `ImageContent` cho screenshot |
| Rủi ro lớn | `execute_lisp`, thao tác file không allowlist, annotation `system`/`view` chưa đúng cho remote |
| HTTP/auth | Chưa có |

### Hai lỗi annotation phải sửa trước khi expose

1. `system` đang có `readOnlyHint=True` nhưng chứa `init` và `execute_lisp`.
2. `view` đang có `readOnlyHint=True` nhưng `zoom_extents` và `zoom_window` làm thay đổi trạng thái viewport.

Trong MVP an toàn, hai tool này phải được đánh dấu là tool ghi. Sau khi cầu nối hoạt động ổn định, có thể thêm các tool chỉ đọc nhỏ, rõ nghĩa như `system_status` và `view_screenshot` để giảm số lần xác nhận mà vẫn giữ 8 tool cũ cho tương thích.

---

## 4. Quyết định kiến trúc

### 4.1. Transport

- `stdio` vẫn là mặc định.
- `streamable-http` là transport chính cho ChatGPT.
- SSE chỉ là fallback nếu quá trình tương thích thực tế cho thấy cần thiết.
- Không nâng major version MCP SDK trong cùng PR với HTTP bridge.
- Trước khi code, phải spike API thật của `mcp==1.26.0`: tên transport, cách tạo ASGI app, endpoint mặc định, session mode và cách gắn middleware.

Ưu tiên tạo một HTTP app/entrypoint riêng thay vì nhồi toàn bộ cấu hình HTTP vào `server.main()`. Các tool handler và backend vẫn dùng chung.

### 4.2. Xác thực

ChatGPT Developer mode không liệt kê “dán một Bearer token tùy ý” như một loại xác thực độc lập. Vì vậy kế hoạch cũ dùng `AUTOCAD_MCP_AUTH_TOKEN` làm đường chính bị loại bỏ.

| Chế độ | Mục đích | Quy tắc |
|--------|----------|---------|
| `none` | Demo E2E ngắn hạn | Phải bật cờ nguy hiểm rõ ràng; tool/operation allowlist rất hẹp; dùng drawing thử; tắt tunnel ngay sau test |
| `oauth` | Vận hành thật | Bắt buộc với remote profile production; kiểm tra issuer, audience, hạn token và scope ở mọi request |
| `mixed` | Tương lai | Chưa cần cho bản đầu; chỉ thêm khi có use case rõ ràng |

Lưu ý: access token OAuth thường được gửi dưới dạng Bearer token ở tầng HTTP, nhưng token đó phải do luồng OAuth cấp; không phải một secret tĩnh tự đặt rồi mong ChatGPT tự chèn header.

### 4.3. OAuth production

Phase OAuth phải có một spike riêng để chọn provider hoặc authorization server tương thích với ChatGPT. Thiết kế tối thiểu:

- Công bố protected-resource metadata theo chuẩn MCP/OAuth mà SDK yêu cầu.
- Công bố authorization-server metadata hoặc OIDC discovery.
- Cấu hình một trong các cách ChatGPT hỗ trợ: static client credentials, CIMD hoặc DCR.
- Xác minh chữ ký/token hoặc dùng introspection nếu token opaque.
- Kiểm tra `issuer`, `audience`, `exp`, `nbf` và scope.
- Scope đề xuất:
  - `autocad.read`
  - `autocad.write`
  - `autocad.admin`
- App ChatGPT bình thường không được cấp `autocad.admin`.
- `execute_lisp` vẫn bị chặn dù token có scope ghi.

Không tự viết một OAuth server đơn giản thiếu kiểm chứng để dùng production. Nếu dùng provider bên ngoài, phải có test discovery, login, refresh và revoke thực tế.

---

## 5. Cấu hình dự kiến

| Biến | Giá trị | Mặc định / quy tắc |
|------|---------|--------------------|
| `AUTOCAD_MCP_TRANSPORT` | `stdio` \| `streamable-http` \| `sse` | `stdio` |
| `AUTOCAD_MCP_HOST` | Địa chỉ bind | `127.0.0.1`; remote không cho bind wildcard |
| `AUTOCAD_MCP_PORT` | Cổng HTTP | `8765` |
| `AUTOCAD_MCP_PATH` | MCP endpoint | `/mcp` sau khi spike xác nhận |
| `AUTOCAD_MCP_PUBLIC_BASE_URL` | URL HTTPS do tunnel cấp | Bắt buộc nếu OAuth metadata cần URL công khai |
| `AUTOCAD_MCP_REMOTE_PROFILE` | `off` \| `dev` \| `production` | `off` |
| `AUTOCAD_MCP_AUTH_MODE` | `none` \| `oauth` | `none`, nhưng production bắt buộc OAuth |
| `AUTOCAD_MCP_ALLOW_NO_AUTH` | Cờ chấp nhận rủi ro dev | `0`; phải set `1` rõ ràng |
| `AUTOCAD_MCP_ALLOWED_DIRS` | Danh sách thư mục, phân tách `;` | Rỗng = chặn mọi operation có path trong remote mode |
| `AUTOCAD_MCP_ALLOWED_HOSTS` | Hostname tunnel được chấp nhận | Bắt buộc khi có public URL ổn định |
| `AUTOCAD_MCP_MAX_IMAGE_BYTES` | Trần dung lượng screenshot | Chốt sau spike ChatGPT E2E |
| `AUTOCAD_MCP_ONLY_TEXT` | Tắt ảnh hoàn toàn | Giữ biến hiện có; dùng để khắc phục sự cố, không force mặc định |
| `AUTOCAD_MCP_BACKEND` | `auto` \| `file_ipc` \| `ezdxf` | Giữ hiện trạng |
| `AUTOCAD_MCP_IPC_TIMEOUT` | Timeout File IPC | Giữ hiện trạng |
| `AUTOCAD_MCP_OAUTH_ISSUER` | OAuth issuer | Bắt buộc ở production |
| `AUTOCAD_MCP_OAUTH_AUDIENCE` | Resource audience | Bắt buộc ở production |
| `AUTOCAD_MCP_OAUTH_SCOPES` | Scope đọc/ghi, phân tách bằng space | `autocad.read autocad.write` |

Các secret OAuth không được lưu trong repo, `.env.example`, script hoặc log.

---

## 6. Quy tắc fail-closed

Khi transport là HTTP, server phải từ chối khởi động nếu:

1. HTTP bind vào `0.0.0.0`, `::` hoặc địa chỉ non-loopback.
2. `REMOTE_PROFILE=production` nhưng `AUTH_MODE` không phải `oauth`.
3. `AUTH_MODE=oauth` nhưng thiếu issuer/audience/cấu hình kiểm tra token.
4. `AUTH_MODE=none` mà không đồng thời có:
   - `REMOTE_PROFILE=dev`
   - `AUTOCAD_MCP_ALLOW_NO_AUTH=1`
5. Public base URL không phải HTTPS, trừ local test không qua tunnel.
6. Allowed host cấu hình không khớp request Host trong production.

Trong remote mode:

- `execute_lisp` luôn bị chặn.
- Path allowlist rỗng nghĩa là chặn `open`, `save` có path, `save_as_dxf` và `plot_pdf`.
- Không có “fallback cho chạy tiếp” khi auth hoặc policy lỗi.
- Token, authorization header và OAuth secret không được ghi log.

---

## 7. Remote guardrails

### 7.1. Operation policy

Tạo policy tập trung, không rải `if REMOTE_MODE` ở nhiều tool handler.

Đầu vào policy:

- transport/profile hiện tại;
- tool + operation;
- OAuth scopes;
- path đã chuẩn hóa;
- mức nguy hiểm của operation.

Kết quả:

- allow;
- deny kèm lỗi dễ hiểu;
- hoặc yêu cầu xác nhận server-side đối với operation nguy hiểm.

### 7.2. Chính sách No Authentication cho demo

Mặc định chỉ quảng bá hoặc chỉ cho chạy nhóm hẹp:

- `system`: `health`, `status`, `get_backend`
- `drawing`: `info`
- `entity`: `list`, `count`, `get`
- `layer`: `list`
- `block`: `list`
- `view`: `get_screenshot`

Nếu cần chứng minh write E2E, chỉ tạm bật `create_line` và `create_circle` trên một drawing thử không chứa thông tin nhạy cảm. Screenshot và `drawing.info` cũng chỉ được dùng với drawing thử vì endpoint no-auth có thể bị bất kỳ ai biết URL gọi. Không bật `open`, `save`, `erase`, `purge`, `execute_lisp`, `drawing.create`, `undo`, `redo` hoặc thao tác hàng loạt trong chế độ no-auth.

No Authentication không được xem là hoàn thành production, dù URL tunnel khó đoán hoặc chỉ dùng trong thời gian ngắn.

### 7.3. Chính sách OAuth

- Scope đọc chỉ chạy operation đọc.
- Scope ghi chạy operation vẽ/sửa thông thường.
- Operation phá hủy hoặc thay file cần xác nhận rõ ràng và audit.
- Không cấp scope admin cho app ChatGPT mặc định.
- `execute_lisp` không được mở lại bằng scope.

### 7.4. Path allowlist bắt buộc

Áp dụng cho `drawing.open`, `drawing.save`, `drawing.save_as_dxf` và `drawing.plot_pdf`:

1. Chuẩn hóa đường dẫn tuyệt đối bằng Windows path semantics.
2. So sánh không phân biệt hoa/thường.
3. Chặn `..`, UNC/network share, device path, alternate data stream và symlink/junction thoát khỏi allowlist.
4. Chỉ cho extension cần thiết: `.dwg`, `.dxf`, `.pdf` theo đúng operation.
5. Nếu `drawing.save` không truyền path, phải xác minh current drawing nằm trong thư mục cho phép.
6. Remote mode không có `ALLOWED_DIRS` thì path operation bị chặn, không phải “không enforce”.

### 7.5. Xác nhận và khôi phục

OpenAI mặc định yêu cầu xác nhận write action, nhưng người dùng có thể nhớ lựa chọn trong một conversation. Vì vậy server vẫn cần lớp bảo vệ riêng cho operation nguy hiểm:

- `drawing.create`
- `drawing.open`
- `drawing.save`
- `purge`
- `erase`
- `undo` / `redo`
- thao tác hàng loạt

Thiết kế khuyến nghị là hai bước `preview -> confirmation_id -> execute`, hoặc ít nhất yêu cầu cờ xác nhận ngắn hạn do server cấp. Confirmation ID phải hết hạn, dùng một lần và gắn với đúng payload.

Trước thao tác phá hủy hoặc thay file, tạo backup/restore point nếu backend hỗ trợ. E2E luôn dùng bản sao drawing, không dùng file công việc gốc.

### 7.6. Screenshot

Không tắt screenshot mặc định vì ảnh là bằng chứng quan trọng khi điều khiển CAD.

- Chỉ tạo ảnh khi caller yêu cầu.
- Giới hạn kích thước pixel và số byte.
- Nếu vượt trần, resize/compress hoặc trả lỗi có hướng dẫn.
- Không log chuỗi base64.
- Dùng `AUTOCAD_MCP_ONLY_TEXT=1` làm chế độ khắc phục timeout/payload, không phải cấu hình production bắt buộc.

### 7.7. Audit và giới hạn tải

Log ra `stderr`:

- request ID;
- profile/auth mode;
- tool + operation;
- allow/deny và lý do;
- thời lượng;
- backend;
- kết quả `ok/error`.

Không log token, secret, raw base64 hoặc toàn bộ AutoLISP. Thêm rate limit theo session/user và giữ File IPC single-flight hiện có.

---

## 8. Tool schema và hướng dẫn cho ChatGPT

### MVP tương thích

- Giữ 8 tool hiện có và tên operation để tránh phá client cũ.
- Sửa `readOnlyHint` sai của `system` và `view` thành `False`.
- Các tool trộn cả đọc và ghi phải được phân loại bảo thủ là write.
- Remote policy quyết định operation nào được phép chạy.
- Tool description phải ghi rõ “Use this when…”, operation được phép, trường hợp bị chặn và thứ tự gọi.

### Remote-safe facade sau MVP

Thêm các tool nhỏ chỉ đọc, không xóa 8 tool cũ:

- `system_status`
- `drawing_info`
- `entity_list`
- `layer_list`
- `block_list`
- `view_screenshot`

Các tool này được đánh dấu `readOnlyHint=True`. Trong ChatGPT app có thể tắt các tool tổng hợp không cần thiết và chỉ bật facade + nhóm write cần dùng.

### Server instructions

Khai báo MCP `instructions` ngắn, trong đó 512 ký tự đầu tự đủ nghĩa:

1. Gọi status trước thao tác AutoCAD.
2. Không yêu cầu/chạy arbitrary AutoLISP.
3. Chỉ làm việc trong allowed directories.
4. Preview trước operation phá hủy.
5. Sau write, chụp ảnh hoặc đọc lại entity để xác minh.
6. Không dùng tool khác khi người dùng chỉ định rõ app AutoCAD MCP.

---

## 9. File dự kiến

```text
src/autocad_mcp/
  server.py                  # stdio entry giữ tương thích
  http_server.py             # NEW: ASGI/Streamable HTTP app factory
  config.py                  # transport + remote/auth config
  remote_policy.py           # NEW: operation/path/scope policy tập trung
  oauth.py                   # NEW ở phase OAuth, hoặc adapter cho provider
  client.py                  # giữ backend singleton; thêm audit hook nếu cần
  ...
scripts/
  run-http-dev.ps1           # no-auth demo, cảnh báo rõ
  run-http-oauth.ps1         # production profile
  run-ngrok.ps1
  run-cloudflare-tunnel.ps1
docs/
  ke-hoach-chatgpt-http-bridge.md
  chatgpt-remote.md
tests/
  test_transport_config.py
  test_streamable_http.py
  test_remote_policy.py
  test_path_guard.py
  test_oauth.py
.env.example                 # chỉ có tên biến/giá trị giả
README.md
```

Tên file OAuth có thể đổi theo SDK/provider sau spike; không khóa cứng thiết kế trước khi xác nhận API.

---

## 10. Kế hoạch triển khai theo phase

### Phase 0 — Baseline và spike

1. Xác nhận root repo này là codebase chuẩn, không vô tình triển khai từ `autocad-mcp-v2/`.
2. `uv sync --locked`.
3. Chạy toàn bộ test hiện có.
4. Ghi baseline test pass/fail trước khi sửa.
5. Spike `mcp==1.26.0`:
   - tạo Streamable HTTP app;
   - endpoint/path;
   - stateful/stateless session;
   - gắn middleware;
   - server instructions;
   - tool annotations.

**Done:** Có một script tối thiểu được MCP client local `initialize -> tools/list -> tools/call` thành công; biết chính xác API cần dùng.

### Phase 1 — Streamable HTTP, không auth, local-only

1. Thêm config transport/host/port/path.
2. Thêm `http_server.py`.
3. Chỉ bind `127.0.0.1`.
4. Giữ `stdio` default 100%.
5. Test protocol bằng MCP client, không chỉ kiểm tra HTTP 200.
6. Test reconnect/session cleanup và hai request đồng thời.

**Done:** Local MCP HTTP list/call được tools; regression stdio pass.

### Phase 2 — Guardrails và annotation

1. Thêm `remote_policy.py`.
2. Chặn `execute_lisp` trong mọi remote profile.
3. Sửa `readOnlyHint` sai.
4. Thêm no-auth safe allowlist.
5. Thêm path guard fail-closed.
6. Thêm audit log và image size guard.
7. Unit test toàn bộ allow/deny matrix.

**Done:** Không thể vượt policy bằng cách đổi operation, path hoặc gọi trực tiếp tool tổng hợp.

### Phase 3 — Tunnel + ChatGPT Plus E2E dev

1. Cài `cloudflared` và chạy `scripts/run-phase3-dev.ps1` với drawing thử. Script
   tự khởi động HTTP server, tạo Quick Tunnel, lấy hostname và khởi động lại
   server với `AUTOCAD_MCP_ALLOWED_HOSTS` đúng hostname tunnel.
2. Mở tunnel tạm thời.
3. ChatGPT: Settings -> Security and login -> Developer mode.
4. Settings -> Plugins -> tạo app bằng URL `https://<tunnel-host>/mcp`.
5. Chọn **No Authentication**.
6. Scan/refresh tool.
7. Trong chat mới, chọn Developer mode app.
8. Test đọc: health -> drawing info -> entity list -> screenshot.
9. Tạm bật đúng một write demo: `create_line` hoặc `create_circle`.
10. Xác minh kết quả bằng screenshot/entity readback.
11. Tắt tunnel và server ngay sau test.

**Done:** Chứng minh ChatGPT Plus web gọi read/write qua MCP, nhưng chưa gọi đây là production-ready.

### Phase 4 — OAuth production

1. Chọn OAuth provider/authorization server bằng spike tương thích.
2. Cấu hình discovery/protected-resource metadata.
3. Cấu hình static client credentials, CIMD hoặc DCR theo lựa chọn thực tế.
4. Validate token + scope.
5. Test login, refresh, expiry, revoke và token sai audience.
6. Bật production profile; xác minh no-auth bị từ chối khởi động.
7. Kết nối lại app ChatGPT bằng OAuth.

Đã triển khai trong repo: OIDC discovery/JWKS JWT verifier, protected-resource
metadata, Bearer challenge và policy `autocad.read`/`autocad.write` trong
`src/autocad_mcp/oauth.py`, cùng script `scripts/run-phase4-oauth.ps1`.
Provider OIDC thật và hostname HTTPS ổn định vẫn cần cấu hình trước khi chạy
E2E login/refresh/revoke với ChatGPT.

**Done:** Chỉ người đã đăng nhập/được cấp scope mới gọi được MCP qua tunnel.

### Phase 5 — Remote UX và operation nguy hiểm

1. Thêm remote-safe read tools nếu cần.
2. Thêm server instructions.
3. Thêm preview/confirmation ID cho operation nguy hiểm.
4. Backup/restore workflow.
5. Test tool selection và confirmation trong nhiều conversation mới.

**Done:** ChatGPT chọn tool rõ hơn; write/destructive flow có cả confirmation của ChatGPT và server.

### Phase 6 — Scripts, docs và vận hành

1. Hoàn thiện PowerShell scripts.
2. Viết `docs/chatgpt-remote.md` bằng tiếng Việt.
3. Cập nhật README.
4. Thêm `.env.example` và gitignore secret/token/cache.
5. Viết runbook mở/tắt server+tunnel, rotate/revoke OAuth và xử lý sự cố.

---

## 11. Ma trận test bắt buộc

| Nhóm | Test |
|------|------|
| Regression | Toàn bộ ezdxf, IPC, screenshot test hiện có |
| Transport | stdio mặc định; Streamable HTTP initialize/list/call; reconnect |
| Startup | Từ chối non-loopback; từ chối production no-auth; thiếu OAuth config |
| No auth | Cần cờ opt-in; operation ngoài safe allowlist bị chặn |
| OAuth | Discovery; token hợp lệ; hết hạn; sai issuer/audience; thiếu scope; revoke |
| Annotation | Tool có side effect không được quảng bá `readOnlyHint=True` |
| Policy | `execute_lisp` luôn bị chặn remote; allow/deny theo operation/scope |
| Path | Traversal, UNC, device path, ADS, junction/symlink escape, extension sai |
| Image | Screenshot hợp lệ; quá trần được resize hoặc từ chối; không log base64 |
| Concurrency | Nhiều HTTP request nhưng File IPC vẫn single-flight, không lẫn request ID |
| Failure | AutoCAD đóng, LISP chưa load, modal dialog, IPC timeout, tunnel đứt |
| E2E | Health, info, screenshot, create entity, readback, undo/backup trên file thử |

CI không yêu cầu AutoCAD; test AutoCAD thật được đánh dấu integration/manual và có checklist riêng.

---

## 12. Definition of Done

### Milestone A — Dev demo

1. `stdio` không bị vỡ.
2. ChatGPT Plus web scan được MCP qua URL tunnel.
3. Read flow thành công.
4. Một write demo giới hạn thành công trên drawing thử.
5. `execute_lisp`, path operation và destructive operation bị chặn ở no-auth profile.
6. Screenshot hoặc entity readback chứng minh thay đổi.

### Milestone B — Production-ready cá nhân

1. OAuth login/refresh/revoke hoạt động.
2. Production profile không thể chạy No Authentication.
3. Scope đọc/ghi được enforce server-side.
4. Path allowlist fail-closed.
5. Annotation write/read đúng; ChatGPT hiển thị confirmation phù hợp.
6. Operation nguy hiểm có server-side confirmation và audit.
7. Không public non-loopback port; tunnel chỉ forward vào localhost.
8. Toàn bộ automated test pass; E2E AutoCAD pass trên bản sao drawing.
9. Có hướng dẫn bật, tắt, revoke và khôi phục khi thao tác sai.

Dự án chỉ được gọi là hoàn chỉnh sau Milestone B. Milestone A chỉ là bằng chứng kỹ thuật.

---

## 13. Rủi ro và giảm thiểu

| Rủi ro | Mức | Giảm thiểu |
|--------|-----|------------|
| No-auth tunnel bị người khác gọi | Rất cao | Chỉ demo ngắn; safe allowlist; drawing thử; tắt tunnel; production bắt buộc OAuth |
| Annotation sai làm mất confirmation | Rất cao | Test annotation; mixed tool đánh dấu write; facade chỉ đọc ở phase sau |
| `execute_lisp` chạy code tùy ý | Rất cao | Chặn cứng ở mọi remote profile, không mở lại bằng scope |
| Mở/lưu file ngoài ý muốn | Cao | Allowed dirs bắt buộc; normalize path; extension allowlist; confirmation |
| OAuth cấu hình sai | Cao | Dùng provider đã kiểm chứng; test issuer/audience/expiry/refresh/revoke |
| Model chọn nhầm operation trong tool tổng hợp | Cao | Description rõ; server instructions; policy; remote-safe facade |
| ChatGPT nhớ approve trong conversation | Cao | Server-side confirmation cho operation nguy hiểm; conversation mới test lại |
| Screenshot quá lớn | Trung bình | Lazy capture; byte/pixel cap; resize; fallback only-text |
| SDK HTTP API thay đổi | Trung bình | Spike/pin lockfile; không nâng major trong bridge PR |
| File IPC timeout/modal dialog | Trung bình | Single-flight hiện có; lỗi rõ; ESC/manual recovery; timeout config |
| URL tunnel thay đổi | Thấp | Script in URL; hướng dẫn refresh/recreate app; không coi URL là secret |

---

## 14. Thứ tự commit/PR đề xuất

1. **PR1:** Baseline + Streamable HTTP local + regression stdio.
2. **PR2:** Remote policy + annotation + path/image/audit guards.
3. **PR3:** Tunnel scripts + ChatGPT Plus no-auth E2E docs.
4. **PR4:** OAuth production + scope tests + runbook revoke/refresh.
5. **PR5:** Remote-safe read tools + preview/confirmation + backup flow.

Không gộp OAuth, tunnel, transport và tool refactor vào một commit lớn; phải giữ khả năng bisect/rollback.

---

## 15. Rollback và dừng khẩn cấp

1. Dừng tunnel process.
2. Dừng HTTP MCP server.
3. Revoke OAuth app/token nếu production.
4. Disable/delete draft app trong ChatGPT Plugins.
5. Khởi động lại server không có `AUTOCAD_MCP_TRANSPORT`; nó quay về `stdio`.
6. Dùng backup/undo để khôi phục drawing thử nếu cần.

Việc thêm HTTP bridge không được thay đổi File IPC protocol hoặc bắt buộc sửa `mcp_dispatch.lsp`, trừ khi integration test chứng minh cần thiết.

---

## 16. Bước bắt đầu implementation

1. Chạy Phase 0 và ghi baseline.
2. Spike API Streamable HTTP của `mcp==1.26.0`.
3. Implement PR1, test local bằng MCP client.
4. Implement PR2 trước khi mở tunnel.
5. Chỉ sau khi guardrails pass mới thực hiện No Authentication E2E.
6. Hoàn thành OAuth trước khi dùng với drawing công việc thật.

Đây là thứ tự bắt buộc: **local transport -> policy/annotation -> tunnel dev -> OAuth -> production**.
