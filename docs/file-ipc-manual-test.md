# File IPC reliability — manual test plan

These checks require native Windows Python and AutoCAD LT 2024 or newer. CI unit
tests mock ActiveX/COM and cannot prove AutoCAD command-line behavior.

Before testing, copy `lisp-code/acadltdoc.lsp.example` to `acadltdoc.lsp`, place
it in an AutoCAD Support File Search Path, and ensure the directory containing
`mcp_dispatch.lsp` is in both Support File Search Path and `TRUSTEDPATHS`.
Restart AutoCAD after changing startup/search-path configuration.

1. Start AutoCAD LT and open a DWG.
2. Confirm the startup message reports MCP Dispatch v3.2 in that document.
3. Call `system(operation="health")`; confirm `dispatcher_reachable=true`, an
   active document name, and a measured `latency_ms`.
4. Call `drawing(operation="info")`.
5. Open a different DWG and call health again. Confirm the new active document
   is reported without using APPLOAD.
6. Insert a block with no attributes.
7. Insert a block with one attribute, then multiple attributes.
8. Repeat with an attribute value containing spaces and Vietnamese Unicode.
9. Request a nonexistent block and a nonexistent attribute tag; confirm a
   structured error and no leftover partial INSERT.
10. Confirm no dialog appears and AutoCAD is never brought to the foreground.
11. After each completed MCP operation, evaluate `CMDACTIVE`; it must return 0.
12. Record initial `ATTDIA`, `ATTREQ`, `FILEDIA`, `CMDECHO`, and `SECURELOAD`;
    trigger both successful and failing operations and verify every value is
    restored exactly.
13. Start a manual AutoCAD command and leave it waiting for input. Call MCP and
    confirm `autocad_busy`; MCP must not send ESC or cancel the user's command.
14. Finish the manual command and call MCP again. It must recover without
    APPLOAD or reloading the dispatcher.
15. Open/new-create another document and confirm `acadltdoc.lsp` loads the
    dispatcher for that document. Temporarily remove startup loading and repeat;
    health must return `dispatcher_missing_in_active_document` rather than a
    generic timeout/not-loaded diagnosis.

Also test a machine where COM routing is unavailable. The backend must return
`command_routing_failed`; it must not silently fall back to Win32 keyboard input.
