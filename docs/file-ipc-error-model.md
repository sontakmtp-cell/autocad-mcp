# File IPC error model

File IPC failures use a stable `error_code`; a timeout alone never proves that
AutoLISP is missing.

| Code | Meaning / action |
|---|---|
| `autocad_not_running` | No running AutoCAD instance/window was found. |
| `no_active_document` | AutoCAD is running without an active drawing. |
| `autocad_busy` | A command is active; finish it and retry. MCP sends no ESC. |
| `modal_dialog_active` | Close the existing AutoCAD dialog; MCP opens none. |
| `dispatcher_missing_in_active_document` | The active document lacks the dispatcher; check per-document `acadltdoc.lsp` startup configuration. |
| `dispatcher_timeout` | Routing succeeded but no valid result arrived before the deadline; this is not automatically a load failure. |
| `active_document_changed` | The active document changed while waiting for a result. |
| `command_routing_failed` | ActiveX `PostCommand` and the explicit `SendCommand` fallback both failed or COM was unavailable. |
| `ipc_result_invalid` | Result JSON or its request/session identifiers were invalid. |
| `command_not_completed` | An MCP-owned AutoCAD subcommand remained pending and was cancelled without touching a pre-existing user command. |
| `block_not_found` / `attribute_tag_not_found` | Block or requested attribute tag does not exist; no partial insert is retained. |

Read-only `ping`/health may retry once after a safe timeout or document-change
race. Write operations are never automatically retried, preventing duplicate
entities.
