# AutoCAD MCP Server

Máy chủ [MCP](https://modelcontextprotocol.io/) cho **tự động hóa AutoCAD LT** và **tạo DXF headless**.

Một API, hai backend vẽ và hai transport kết nối:

| | Tùy chọn | Ghi chú |
|---|---|---|
| **Backend** | **File IPC** | Windows + AutoCAD LT 2024+; điều khiển qua ActiveX/COM + AutoLISP |
| | **ezdxf** | Mọi nền tảng; DXF in-memory, không cần AutoCAD |
| **Transport** | **stdio** (mặc định) | Claude Desktop, Claude Code, client MCP cục bộ |
| | **streamable-http** | ChatGPT Developer mode / remote MCP qua HTTPS tunnel |

Server cung cấp **8 công cụ** (`drawing`, `entity`, `layer`, `block`, `annotation`, `pid`, `view`, `system`) với dispatch theo `operation`. Phiên bản runtime: **3.1** (LISP dispatcher reliability **v3.2**).

---

## Prerequisites

### Backend File IPC (AutoCAD thật)

- **Windows 10/11** (không dùng WSL Python cho File IPC)
- **AutoCAD LT 2024+** trên Windows — AutoLISP chỉ có từ LT 2024; **AutoCAD LT cho Mac không hỗ trợ AutoLISP**
- **Python 3.10+** (native Windows)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** package manager

### Backend ezdxf (headless)

Chạy trên Linux, macOS, WSL hoặc Windows — **không cần AutoCAD**, chỉ tạo/sửa DXF offline.

---

## Quick Start

### 1. Clone và cài đặt

```powershell
git clone https://github.com/sontakmtp-cell/autocad-mcp.git
cd autocad-mcp
uv sync
```

### 2. Auto-load LISP dispatcher (mỗi document)

State AutoLISP thuộc từng bản vẽ. Load `mcp_dispatch.lsp` một lần không đủ khi mở/tạo DWG mới. Cấu hình startup theo document:

1. Thêm `<repo>/lisp-code` vào **Support File Search Path**.
2. Thêm cùng thư mục vào `TRUSTEDPATHS`. **Không** tắt `SECURELOAD`.
3. Copy `lisp-code/acadltdoc.lsp.example` thành `acadltdoc.lsp` trong một Support File Search Path.
4. Restart AutoCAD LT, mở bản vẽ. Mỗi document nên in:

```text
=== MCP Dispatch v3.2 reliability overrides loaded ===
```

Example dùng `(findfile "mcp_dispatch.lsp")` rồi `(load ...)`, không hard-code path máy. Startup Suite / APPLOAD lặp lại không cần. Nếu document đang active thiếu dispatcher, MCP báo `dispatcher_missing_in_active_document` — server **không** tự mở APPLOAD.

### 3. Cấu hình MCP client (stdio)

Ví dụ `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "C:\\path\\to\\autocad-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "autocad_mcp"],
      "env": { "AUTOCAD_MCP_BACKEND": "auto" }
    }
  }
}
```

- `command` phải trỏ tới **Windows Python trong venv**
- Không dùng WSL Python khi cần File IPC

#### Client chạy trong WSL

Khởi động server qua `cmd.exe` + Windows Python:

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "type": "stdio",
      "command": "cmd.exe",
      "args": [
        "/d",
        "/s",
        "/c",
        "cd /d C:\\path\\to\\autocad-mcp && .venv\\Scripts\\python.exe -m autocad_mcp"
      ],
      "env": { "AUTOCAD_MCP_BACKEND": "auto" }
    }
  }
}
```

### 4. Kiểm tra

Từ MCP client:

```text
system(operation="status")
```

- `backend: "file_ipc"` — AutoCAD đang chạy và được phát hiện
- `backend: "ezdxf"` — chế độ headless

Diagnostics bổ sung:

```text
system(operation="health")
system(operation="tool_manifest")
system(operation="runtime")
```

---

## Transport HTTP (ChatGPT / remote)

Transport mặc định vẫn là **stdio**. Để mở MCP qua mạng:

| Profile | Auth | Mục đích |
|---|---|---|
| `off` | — | Chỉ local; stdio hoặc HTTP không remote policy |
| `dev` | No Auth (`ALLOW_NO_AUTH=1`) | Demo ngắn hạn, **chỉ operation đọc** allowlist |
| `production` | OAuth 2.1 / OIDC | Remote thật (ChatGPT, tunnel HTTPS) |

### Dev demo (No Authentication)

```powershell
$env:AUTOCAD_MCP_BACKEND = "ezdxf"   # hoặc auto / file_ipc
$env:AUTOCAD_MCP_TRANSPORT = "streamable-http"
$env:AUTOCAD_MCP_HOST = "127.0.0.1"
$env:AUTOCAD_MCP_PORT = "8765"
$env:AUTOCAD_MCP_PATH = "/mcp"
$env:AUTOCAD_MCP_REMOTE_PROFILE = "dev"
$env:AUTOCAD_MCP_AUTH_MODE = "none"
$env:AUTOCAD_MCP_ALLOW_NO_AUTH = "1"
$env:AUTOCAD_MCP_ALLOWED_HOSTS = "127.0.0.1"

uv run python -m autocad_mcp
```

Hoặc dùng script tunnel Phase 3:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-phase3-dev.ps1
```

### Production OAuth + ChatGPT

Server là **resource server** (không tự host login). Cần OIDC provider (Auth0, Okta, Cognito, …) phát hành JWT + JWKS.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-phase4-oauth.ps1 `
  -PublicBaseUrl "https://cad.example.com" `
  -OAuthIssuer "https://issuer.example" `
  -OAuthAudience "https://cad.example.com"
```

- Endpoint MCP: `https://cad.example.com/mcp`
- Metadata: `https://cad.example.com/.well-known/oauth-protected-resource`
- Scopes: `autocad.read`, `autocad.write`
- `execute_lisp` **mặc định bị chặn** trên mọi remote profile; chỉ bật khi set
  `AUTOCAD_MCP_ALLOW_EXECUTE_LISP=1` (rủi ro cao — AutoLISP tùy ý trên máy host)

#### Kết nối ChatGPT (Windows — khuyến nghị)

ChatGPT gọi MCP qua **HTTPS công khai**. MCP chỉ bind `127.0.0.1:8765`; cần **Cloudflare named tunnel** (không dùng Quick Tunnel `*.trycloudflare.com` cho OAuth domain cố định).

**Cài cloudflared** (một lần):

```powershell
winget install --id Cloudflare.cloudflared
```

**Mỗi phiên làm việc — 2 cửa sổ:**

| Cửa sổ | File | Việc |
|---|---|---|
| 1 | `start_mcp_chatgpt.bat` | MCP OAuth production trên `127.0.0.1:8765` |
| 2 | `start_cloudflare_tunnel.bat` | Login Cloudflare (lần đầu) → tạo tunnel/config/DNS → `tunnel run` |

`start_cloudflare_tunnel.bat` gọi `scripts/setup-cloudflare-tunnel.ps1`:

1. Mở browser / in link **Cloudflare login** nếu chưa có `~\.cloudflared\cert.pem`
2. Chọn zone domain (ví dụ `kythuatvang.com`) → **Authorize**
3. Tạo/chọn named tunnel, ghi `config.yml` (`hostname` → `http://127.0.0.1:8765`)
4. Route DNS (ghi đè record cũ bằng CNAME tunnel nếu cần)
5. Chạy tunnel (giữ cửa sổ mở)

URL gắn vào ChatGPT Developer mode (OAuth):

```text
https://cad.kythuatvang.com/mcp
```

(Wrapper `start_mcp_chatgpt.bat` đang cấu hình site-specific cho domain/issuer Auth0 của repo này — chỉnh bat/script nếu dùng domain khác.)

**Kiểm tra public trước khi connect ChatGPT:**

```powershell
curl.exe -sS -i "https://cad.kythuatvang.com/.well-known/oauth-protected-resource"
# kỳ vọng: 200 + JSON scopes/issuer

curl.exe -sS -i "https://cad.kythuatvang.com/mcp"
# kỳ vọng: 401 khi chưa có token (OAuth bật)
```

| HTTP | Ý nghĩa |
|---|---|
| **530** | Tunnel chưa `run` / DNS chưa gắn named tunnel |
| **502** | Tunnel OK nhưng MCP (`:8765`) chưa chạy |
| **401** trên `/mcp` | Đúng — client phải login OAuth |
| **200** metadata | Resource server reachable |

Tuỳ chọn PowerShell:

```powershell
# Chỉ setup, không giữ tunnel
powershell -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare-tunnel.ps1 -SkipRun

# Hostname / tunnel name tuỳ chỉnh
powershell -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare-tunnel.ps1 `
  -Hostname "cad.example.com" -TunnelName "autocad-mcp"
```

Chi tiết OAuth/policy: [docs/phase2-remote-policy.md](docs/phase2-remote-policy.md), [docs/phase4-oauth.md](docs/phase4-oauth.md), [docs/ke-hoach-chatgpt-http-bridge.md](docs/ke-hoach-chatgpt-http-bridge.md).

> `AUTOCAD_MCP_TRANSPORT=sse` được nhận trong config nhưng **chưa implement** — dùng `stdio` hoặc `streamable-http`.

---

## Tools

Mỗi tool nhận `operation` (string) và thường có `data` (object) + `include_screenshot` (bool).

### drawing — Quản lý file bản vẽ

| Operation | Mô tả | File IPC | ezdxf |
|---|---|---|---|
| `create` | Bản vẽ mới / reset | Yes | Yes |
| `open` | Mở bản vẽ | Yes | Yes (DXF) |
| `info` | Extents, entity, layer, block | Yes | Yes |
| `save` | Lưu (`path?`) | Yes | Yes |
| `save_as_dxf` | Xuất DXF | Yes | Yes |
| `plot_pdf` | Xuất PDF | Yes | No |
| `purge` | Xóa đối tượng không dùng | Yes | Yes |
| `get_variables` | Đọc biến hệ thống | Yes | Yes |
| `undo` / `redo` | Hoàn tác / làm lại | Yes | No |

### entity — Đối tượng hình học

**Tạo:** `create_line`, `create_circle`, `create_polyline`, `create_rectangle`, `create_arc`, `create_ellipse`, `create_mtext`, `create_hatch`

**Đọc:** `list`, `count`, `get`

**Sửa:** `copy`, `move`, `rotate`, `scale`, `mirror`, `offset`\*, `array`, `fillet`\*, `chamfer`\*, `erase`

\* `offset`, `fillet`, `chamfer` chỉ File IPC.

### layer — Layer

`list`, `create`, `set_current`, `set_properties`, `freeze`, `thaw`, `lock`, `unlock`

### block — Block

| Operation | File IPC | ezdxf |
|---|---|---|
| `list` | Yes | Yes |
| `insert` | Yes | Yes |
| `insert_with_attributes` | Yes | Yes |
| `get_attributes` | Yes | Yes |
| `update_attribute` | Yes | Yes |
| `define` | No | Yes |

### annotation — Chú thích & auto-dimension

**Cơ bản**

- `create_text`
- `create_dimension_linear` / `aligned` / `angular` / `radius`
- `create_leader`

**Workflow dimension tự động (part-aware)**

| Operation | Vai trò |
|---|---|
| `detect_parts` | Gom geometry 2D thành `part_1`, `part_2`, … (read-only) |
| `plan_dimensions` | Preview plan `D1`, `D2`, … — **không** sửa bản vẽ |
| `commit_dimension_plan` | Ghi plan đã duyệt (một Undo group trên File IPC) |
| `auto_dimension` | Lối tắt detect → plan → commit |
| `batch_create_dimensions` | Ghi nhiều dimension một request / một Undo group |
| `dimension_profiles` | `list` / `get` / `save` / `delete` profile mm·inch·ISO |
| `audit_dimensions` | Kiểm tra chất lượng bố trí (read-only) |
| `repair_dimension_layout` | Sửa an toàn theo `audit_id` (trùng lặp, layer, lane) |

Selector cho plan/auto (chọn **một**): `target_part_id` | `entity_ids` | `region` | `selection: "current"`.

Profile built-in: `mechanical_mm`, `mechanical_inch`, `iso_simple`. Profile tùy chỉnh mặc định lưu tại `%LOCALAPPDATA%\autocad-mcp\dimension_profiles.json` (hoặc `AUTOCAD_MCP_DIMENSION_PROFILES`).

Chi tiết và ví dụ JSON: [docs/auto-dimension.md](docs/auto-dimension.md).

### pid — P&ID (thư viện CTO)

`setup_layers`, `insert_symbol`, `list_symbols`, `draw_process_line`, `connect_equipment`, `add_flow_arrow`, `add_equipment_tag`, `add_line_number`, `insert_valve`, `insert_instrument`, `insert_pump`, `insert_tank`

Cần cài thư viện [CAD Tools Online](https://www.cadtoolsonline.com/) vào:

```text
C:\PIDv4-CTO\
```

### view — Viewport & screenshot

| Operation | Mô tả |
|---|---|
| `zoom_extents` | Zoom toàn bộ |
| `zoom_window` | Zoom cửa sổ `x1,y1,x2,y2` |
| `get_screenshot` | Chụp PNG |

- File IPC: Win32 `PrintWindow` (kể cả khi AutoCAD minimize)
- ezdxf: matplotlib render

### system — Server

| Operation | Mô tả |
|---|---|
| `status` / `get_backend` | Backend, capabilities |
| `health` | Health check nhanh (có `error_code` ổn định) |
| `runtime` | Platform, Python path, backend env |
| `tool_manifest` | Tools đã đăng ký, annotation ops, feature status |
| `init` | Re-init backend |
| `execute_lisp` | Chạy AutoLISP tùy ý — File IPC; remote cần `AUTOCAD_MCP_ALLOW_EXECUTE_LISP=1` |

Ví dụ `execute_lisp`:

```text
system(operation="execute_lisp", data={"code": "(+ 1 2)"})
```

---

## Architecture

```text
MCP Client (Claude / ChatGPT / …)
        │
        ├── stdio (JSON-RPC)          ← mặc định
        └── streamable-http (/mcp)    ← remote + tunnel HTTPS
                │
                ▼
        remote policy / OAuth / path guard
                │
                ▼
     Python MCP Server (autocad_mcp 3.1)
                │
        ├── File IPC Backend (Windows)
        │         │  C:/temp/*session*/*request*.json
        │         ▼
        │   mcp_dispatch.lsp (AutoCAD LT)
        │   + auto_dimension*.lsp
        │
        └── ezdxf Backend
                  ▼
             in-memory DXF
```

**File IPC**

- Route expression whitelist qua ActiveX: ưu tiên `Document.PostCommand`, fallback tường minh `Document.SendCommand`
- **Không** dùng keyboard, clipboard, focus hay `WM_CHAR`
- Trước khi gửi: kiểm tra `IsQuiescent` / `CMDACTIVE` → `autocad_busy` nếu user đang có lệnh (MCP **không** gửi ESC)
- Request file mang process/session ID + request ID; LISP mở đúng file đó

Mã lỗi ổn định: [docs/file-ipc-error-model.md](docs/file-ipc-error-model.md).  
Checklist tay: [docs/file-ipc-manual-test.md](docs/file-ipc-manual-test.md).

---

## Environment Variables

### Backend & IPC

| Variable | Default | Mô tả |
|---|---|---|
| `AUTOCAD_MCP_BACKEND` | `auto` | `auto` \| `file_ipc` \| `ezdxf` |
| `AUTOCAD_MCP_IPC_DIR` | `C:/temp` | Thư mục file IPC |
| `AUTOCAD_MCP_IPC_TIMEOUT` | `10` | Timeout giây (clamp 1–300) |
| `AUTOCAD_MCP_COM_PROGID` | (auto) | Override COM ProgID AutoCAD đang chạy |
| `AUTOCAD_MCP_ONLY_TEXT` | `false` | Tắt đính kèm screenshot |
| `AUTOCAD_MCP_DIMENSION_PROFILES` | `%LOCALAPPDATA%\…` | Path file profile dimension tùy chỉnh |
| `AUTOCAD_MCP_DEBUG_DETECT_FILE` | — | Ghi snapshot debug backend detection |
| `AUTOCAD_MCP_ENTRYPOINT` | (auto) | Nhãn entrypoint trong `tool_manifest` |

Nếu đổi `AUTOCAD_MCP_IPC_DIR`, cập nhật tương ứng biến `*mcp-ipc-dir*` trong `lisp-code/mcp_dispatch.lsp`.

### Transport & remote

| Variable | Default | Mô tả |
|---|---|---|
| `AUTOCAD_MCP_TRANSPORT` | `stdio` | `stdio` \| `streamable-http` (`sse` chưa implement) |
| `AUTOCAD_MCP_HOST` | `127.0.0.1` | Bind host HTTP |
| `AUTOCAD_MCP_PORT` | `8765` | Port HTTP |
| `AUTOCAD_MCP_PATH` | `/mcp` | Path MCP endpoint |
| `AUTOCAD_MCP_STATELESS_HTTP` | `false` | Stateless HTTP mode |
| `AUTOCAD_MCP_REMOTE_PROFILE` | `off` | `off` \| `dev` \| `production` |
| `AUTOCAD_MCP_AUTH_MODE` | `none` | `none` \| `oauth` |
| `AUTOCAD_MCP_ALLOW_NO_AUTH` | `false` | Bật No Auth (chỉ với profile `dev`) |
| `AUTOCAD_MCP_ALLOW_EXECUTE_LISP` | `false` | Cho phép `system.execute_lisp` qua remote (opt-in; rủi ro cao) |
| `AUTOCAD_MCP_ALLOWED_HOSTS` | — | Host allowlist (`;`-separated) |
| `AUTOCAD_MCP_ALLOWED_DIRS` | — | Thư mục file cho open/save remote (`;`-separated) |
| `AUTOCAD_MCP_PUBLIC_BASE_URL` | — | Resource URL HTTPS công khai (OAuth) |
| `AUTOCAD_MCP_MAX_IMAGE_BYTES` | `5242880` | Giới hạn ảnh remote (5 MB) |
| `AUTOCAD_MCP_OAUTH_ISSUER` | — | OIDC issuer |
| `AUTOCAD_MCP_OAUTH_AUDIENCE` | — | JWT `aud` |
| `AUTOCAD_MCP_OAUTH_SCOPES` | `autocad.read autocad.write` | Scopes chấp nhận |

---

## Development

```powershell
uv sync
uv run pytest tests/ -v
```

CI: `.github/workflows/test.yml`.

Script hỗ trợ:

| Script | Mục đích |
|---|---|
| `scripts/run-phase3-dev.ps1` | HTTP dev + Cloudflare **Quick Tunnel** demo (No Auth) |
| `scripts/run-phase4-oauth.ps1` | HTTP production OAuth (local `:8765`) |
| `scripts/setup-cloudflare-tunnel.ps1` | Login CF + named tunnel + DNS + `tunnel run` |
| `start_mcp_chatgpt.bat` | Wrapper Phase 4 OAuth (site-specific) |
| `start_cloudflare_tunnel.bat` | Wrapper named tunnel cho ChatGPT HTTPS |

---

## AutoCAD LT AutoLISP Compatibility

AutoLISP có trên **AutoCAD LT 2024+ (Windows)**.

| Hỗ trợ | Không hỗ trợ |
|---|---|
| `.lsp`, `vl-*`, file I/O, `entget`, selection sets | VLIDE, Express Tools đầy đủ, 3D nâng cao, AutoLISP trên Mac |

Một số đường dimension tối ưu dùng ActiveX khi có; xem `lisp-code/auto_dimension_activex.lsp`.

---

## Documentation

| Tài liệu | Nội dung |
|---|---|
| [docs/auto-dimension.md](docs/auto-dimension.md) | Workflow part-aware dimension |
| [docs/file-ipc-error-model.md](docs/file-ipc-error-model.md) | Mã lỗi File IPC |
| [docs/file-ipc-manual-test.md](docs/file-ipc-manual-test.md) | Kiểm thử tay IPC |
| [docs/phase2-remote-policy.md](docs/phase2-remote-policy.md) | Remote allowlist / path guard |
| [docs/phase4-oauth.md](docs/phase4-oauth.md) | OAuth production + ChatGPT |
| [docs/ke-hoach-chatgpt-http-bridge.md](docs/ke-hoach-chatgpt-http-bridge.md) | Kế hoạch HTTP bridge tổng thể |
| [docs/phase0-baseline.md](docs/phase0-baseline.md) | Baseline phase 0 |

---

## What's included (current)

- 8 tool MCP thống nhất, operation dispatch
- Dual backend: File IPC + ezdxf
- Dual transport: stdio + Streamable HTTP
- Remote policy (dev No-Auth allowlist, production OAuth scopes)
- ChatGPT path: OAuth MCP + Cloudflare **named** tunnel helpers (`start_cloudflare_tunnel.bat`)
- Part-aware auto-dimension: detect → plan → commit / one-shot / audit-repair
- File IPC reliability: session/request IDs, structured `error_code`, per-document dispatcher probe
- `system.tool_manifest` / `runtime` / `health` diagnostics
- `execute_lisp` cho automation mở rộng (local only)
- Wheel packaging kèm `lisp-code` (`hatchling` force-include)

---

## License

MIT
