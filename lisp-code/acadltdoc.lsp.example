;;; Place a copy named acadltdoc.lsp in a folder listed in Support File Search Path.
;;; Add the folder containing mcp_dispatch.lsp to TRUSTEDPATHS.
;;; AutoCAD loads acadltdoc.lsp once for every opened or newly created document.

(setq mcp-dispatch-path (findfile "mcp_dispatch.lsp"))
(if mcp-dispatch-path
  (load mcp-dispatch-path)
  (princ "\nMCP: mcp_dispatch.lsp was not found in Support File Search Path."))
(princ)
