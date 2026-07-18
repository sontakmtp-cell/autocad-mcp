# Phase 0 — Baseline và kết quả spike

Ngày thực hiện: 2026-07-18  
Repo chuẩn: `D:\AI\autocad-mcp`

## 1. Xác nhận codebase

- Root đang làm việc là `D:\AI\autocad-mcp`.
- Package chính nằm ở `src\autocad_mcp`.
- Không sử dụng một thư mục `autocad-mcp-v2` khác.
- Entry point hiện tại là `python -m autocad_mcp`, gọi `server.main()` và chạy
  `mcp.run(transport="stdio")`.

## 2. Đồng bộ môi trường

Lệnh:

```powershell
uv sync --locked
```

Kết quả: thành công. Môi trường `.venv` có MCP SDK `1.26.0` và `ezdxf 1.4.3`.

## 3. Baseline test trước khi sửa

### Lệnh theo README

```powershell
uv run pytest tests/ -v
```

Kết quả: không thu thập được test, với 3 lỗi import:

- `ezdxf` không được tìm thấy.
- `autocad_mcp` không được tìm thấy.
- Process pytest được gọi từ Python hệ thống thay vì Python của `.venv`.

Nguyên nhân môi trường: pytest và pytest-asyncio chưa được khai báo trong một
dependency group/extra hợp lệ của `pyproject.toml`, nên `uv sync --locked`
không đưa chúng vào `.venv`; lệnh `uv run pytest` đã gọi executable pytest nằm
ngoài môi trường project.

### Lệnh kiểm tra có kiểm soát

Để tách lỗi môi trường khỏi lỗi code, chạy pytest bằng Python của project và
nạp tạm các công cụ test:

```powershell
uv run --with pytest --with pytest-asyncio python -m pytest tests/ -v
```

Kết quả baseline trước khi thêm spike:

```text
123 passed, 7 warnings in 3.92s
```

Các warning đến từ API cũ bên trong `ezdxf.queryparser`, không phải test fail.

## 4. Spike Streamable HTTP

File chạy thử:

```text
scripts/phase0_streamable_http_spike.py
```

Spike dùng trực tiếp API của `mcp==1.26.0` và không sửa entrypoint stdio hoặc
backend thật. Nó chứng minh:

- tạo `FastMCP(..., streamable_http_path="/mcp")`;
- tạo ASGI app bằng `server.streamable_http_app()`;
- bind server vào `127.0.0.1`;
- gắn Starlette middleware bằng `app.add_middleware(...)`;
- truyền `instructions` ở constructor `FastMCP`;
- đọc `readOnlyHint` và các annotation từ kết quả `tools/list`;
- MCP client thật gọi được `initialize`, `tools/list`, `tools/call`;
- chạy được cả session stateful (`stateless_http=False`) và stateless
  (`stateless_http=True`).

Chạy stateful:

```powershell
uv run python scripts/phase0_streamable_http_spike.py --port 8765
```

Chạy stateless:

```powershell
uv run python scripts/phase0_streamable_http_spike.py --port 8766 --stateless
```

Cả hai lần đều trả về các kiểm tra sau là `true`:

```json
{
  "initialize": true,
  "tools_list": true,
  "tools_call": true,
  "middleware": true,
  "instructions": true,
  "annotation": true
}
```

### API đã xác nhận cho Phase 1

```python
server = FastMCP(
    "autocad-mcp",
    instructions="...",
    host="127.0.0.1",
    port=8765,
    streamable_http_path="/mcp",
    stateless_http=False,
)
app = server.streamable_http_app()
app.add_middleware(YourMiddleware)
```

Client local dùng:

```python
async with streamable_http_client("http://127.0.0.1:8765/mcp") as (
    read_stream,
    write_stream,
    get_session_id,
):
    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()
        await session.list_tools()
        await session.call_tool("tool_name", {"argument": "value"})
```

Kết luận: API cần cho Phase 1 đã được xác nhận trên đúng SDK đang khóa; chưa
cần nâng major version MCP và chưa thay đổi server production.
