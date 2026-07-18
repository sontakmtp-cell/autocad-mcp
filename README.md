# AutoCAD MCP Server

Máy chủ MCP cho **tự động hóa AutoCAD LT** và **tạo DXF headless**.

Hai backend, một API:

| Backend | Runtime | Cần AutoCAD? | Screenshot |
|---------|---------|-------------|------------|
| **File IPC** | Windows Python | Có — AutoCAD LT 2024+ (Windows) | Win32 PrintWindow |
| **ezdxf** | Mọi nền tảng | Không (headless) | matplotlib render |

Server cung cấp **8 công cụ chính** (`drawing`, `entity`, `layer`, `block`, `annotation`, `pid`, `view`, `system`) thông qua **MCP stdio transport**.  

Một **MCP client** (Claude Desktop, Claude Code, v.v.) có thể kết nối và điều khiển AutoCAD bằng **yêu cầu ngôn ngữ tự nhiên**.

---

# Prerequisites (Backend File IPC)

Yêu cầu:

- **Windows 10/11**  
  (Backend File IPC dùng AutoCAD ActiveX/COM mà không cần focus cửa sổ)

- **AutoCAD LT 2024 trở lên**  
  AutoLISP được hỗ trợ từ LT 2024 trên Windows.  

  ⚠ AutoCAD LT cho Mac **không hỗ trợ AutoLISP**.

- **Python 3.10+**  
  (Python chạy native trên Windows — **không dùng WSL Python**)

- **uv package manager**

Cài đặt:

https://docs.astral.sh/uv/getting-started/installation/

---

💡 Backend **ezdxf headless** chạy được trên:

- Linux
- macOS
- WSL

và **không cần AutoCAD**, chỉ dùng để tạo file DXF offline.

---

# Quick Start

## 1. Clone và cài đặt

```powershell
git clone https://github.com/ks40-academy/autocad-mcp.git
cd autocad-mcp
uv sync
```

---

# 2. Auto-load LISP dispatcher for every document

AutoLISP state belongs to each AutoCAD document. Loading `mcp_dispatch.lsp` in
one DWG does not guarantee it exists after opening or creating another DWG.
Configure document startup once instead of using APPLOAD as recovery:

1. Add `<repo>/lisp-code` to **Support File Search Path**.
2. Add the same directory to `TRUSTEDPATHS`. Do not disable `SECURELOAD`.
3. Copy `lisp-code/acadltdoc.lsp.example` to `acadltdoc.lsp` in a Support File
   Search Path.
4. Restart AutoCAD LT and open a drawing. Each document should print:

```text
=== MCP Dispatch v3.2 reliability overrides loaded ===
```

The example uses `(findfile "mcp_dispatch.lsp")` followed by `(load ...)`, so it
contains no machine-specific path. Startup Suite and repeated APPLOAD are not
needed. If startup is missing in a newly active document, MCP reports
`dispatcher_missing_in_active_document`; it never opens APPLOAD automatically.

---

# 3. Cấu hình MCP client

Ví dụ cấu hình trong:

`claude_desktop_config.json`

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

### Lưu ý quan trọng

- `command` phải trỏ tới **Windows Python trong venv**
- Không dùng **WSL Python**

---

# Chạy từ WSL

Nếu MCP client chạy trong WSL (ví dụ Claude Code):

khởi động server thông qua `cmd.exe`

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

---

# 4. Kiểm tra hoạt động

Từ MCP client gọi:

```
system(operation="status")
```

Kết quả:

```
backend: "file_ipc"
```

nếu AutoCAD đang chạy

hoặc

```
backend: "ezdxf"
```

nếu chạy chế độ headless.

---

# Tools

## drawing — Quản lý file bản vẽ

| Operation | Mô tả | File IPC | ezdxf |
|-----------|------|----------|-------|
| create | Reset bản vẽ sạch | Yes | Yes |
| open | Mở bản vẽ | Yes | Yes (DXF) |
| info | Thông tin entity và layer | Yes | Yes |
| save | Lưu bản vẽ | Yes | Yes |
| save_as_dxf | Xuất DXF | Yes | Yes |
| plot_pdf | Xuất PDF | Yes | No |
| purge | Xóa đối tượng không dùng | Yes | Yes |
| get_variables | Lấy biến hệ thống | Yes | Yes |
| undo | Hoàn tác | Yes | No |
| redo | Làm lại | Yes | No |

---

# entity — Quản lý đối tượng

### Tạo

- create_line
- create_circle
- create_polyline
- create_rectangle
- create_arc
- create_ellipse
- create_mtext
- create_hatch

### Đọc

- list
- count
- get

### Chỉnh sửa

- copy
- move
- rotate
- scale
- mirror
- offset*
- array
- fillet*
- chamfer*
- erase

⚠ `offset`, `fillet`, `chamfer` chỉ dùng với File IPC.

---

# layer — Quản lý layer

- list
- create
- set_current
- set_properties
- freeze
- thaw
- lock
- unlock

---

# block — Thao tác block

| Operation | File IPC | ezdxf |
|----------|----------|-------|
| list | Yes | Yes |
| insert | Yes | Yes |
| insert_with_attributes | Yes | Yes |
| get_attributes | Yes | Yes |
| update_attribute | Yes | Yes |
| define | No | Yes |

---

# annotation — Chú thích

- create_text
- create_dimension_linear
- create_dimension_aligned
- create_dimension_angular
- create_dimension_radius
- create_leader

---

# pid — P&ID

Thư viện ký hiệu CTO.

Các lệnh:

- setup_layers
- insert_symbol
- list_symbols
- draw_process_line
- connect_equipment
- add_flow_arrow
- add_equipment_tag
- add_line_number
- insert_valve
- insert_instrument
- insert_pump
- insert_tank

---

⚠ Cần cài thư viện:

https://www.cadtoolsonline.com/

vào thư mục

```
C:\PIDv4-CTO\
```

---

# view — Viewport & Screenshot

| Operation | Mô tả |
|-----------|------|
| zoom_extents | Zoom toàn bộ |
| zoom_window | Zoom theo cửa sổ |
| get_screenshot | Chụp ảnh AutoCAD |

File IPC dùng:

```
PrintWindow (Win32)
```

Có thể chụp ngay cả khi AutoCAD bị minimize.

ezdxf dùng:

```
matplotlib render
```

---

# system — Quản lý server

- status
- health
- get_backend
- runtime
- init
- execute_lisp

---

## execute_lisp

Chạy AutoLISP bất kỳ:

Ví dụ

```
(+ 1 2)
```

Gửi:

```
data: {code: "(+ 1 2)"}
```

Biến server thành **nền tảng automation mở rộng**.

---

# Architecture

```
MCP Client (Claude)
        │
        │ stdio (JSON-RPC)
        ▼
Python MCP Server (autocad_mcp)
        │
        ├── File IPC Backend
        │       │
        │       └── C:/temp/*.json
        │               │
        │               ▼
        │        mcp_dispatch.lsp (AutoCAD)
        │
        └── ezdxf Backend
                │
                ▼
           in-memory DXF
```

File IPC routes a fixed, whitelisted dispatcher expression through AutoCAD's
ActiveX/COM document API. The primary route is `Document.PostCommand`, which
queues execution for an idle document. `Document.SendCommand` is an explicit
API fallback when `PostCommand` is unavailable; there is no silent keyboard,
clipboard, focus, or Win32 `WM_CHAR` fallback.

Before routing, the backend checks `GetAcadState().IsQuiescent` and
`CMDACTIVE`. A pre-existing user command returns `autocad_busy`; MCP sends no
ESC. IPC request files include both a process/session ID and request ID, and the
LISP entry point opens that exact file instead of selecting an arbitrary pending
file.

See `docs/file-ipc-error-model.md` and `docs/file-ipc-manual-test.md`.

---

# Environment Variables

| Variable | Default | Mô tả |
|---------|--------|------|
| AUTOCAD_MCP_BACKEND | auto | chọn backend |
| AUTOCAD_MCP_IPC_DIR | C:/temp | thư mục IPC |
| AUTOCAD_MCP_IPC_TIMEOUT | 10 | timeout |
| AUTOCAD_MCP_COM_PROGID | auto | optional running AutoCAD COM ProgID override |
| AUTOCAD_MCP_ONLY_TEXT | false | tắt screenshot |

---

⚠ Nếu đổi:

```
AUTOCAD_MCP_IPC_DIR
```

phải sửa luôn biến:

```
*mcp-ipc-dir*
```

trong file

```
mcp_dispatch.lsp
```

---

# Development

```powershell
uv sync
uv run pytest tests/ -v
```

---

# AutoCAD LT AutoLISP Compatibility

AutoLISP được thêm vào **AutoCAD LT 2024 (Windows)**.

| Hỗ trợ | Không hỗ trợ |
|------|-------------|
| .lsp | VLIDE |
| vl-* functions | vlax-* |
| File I/O | Express Tools |
| entget | 3D operations |
| selection sets | AutoLISP trên Mac |

---

# What's New v3.1

Các cập nhật chính:

- execute_lisp
- undo / redo
- open drawing
- create drawing reset
- save path
- get_variables fix
- polyline fix
- ActiveX/COM dispatch without UI keystrokes or ESC
- Structured File IPC errors and per-document dispatcher probes
- UTF8 fallback
- IPC timeout config
- thread-safe init

---

# License

MIT