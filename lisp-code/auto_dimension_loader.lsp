;;; Lightweight, version-aware loader for auto_dimension.lsp.
;;; The dispatcher may load this tiny file for each annotation IPC request, but
;;; the heavy engine is parsed only once per AutoCAD document/version.

(setq mcp-ad-loader-target-version "phase1-2026-07-19")

(if
  (or
    (not (boundp '*mcp-auto-dimension-loader-version*))
    (/= *mcp-auto-dimension-loader-version* mcp-ad-loader-target-version)
  )
  (progn
    (setq mcp-ad-engine-path (findfile "auto_dimension.lsp"))
    (if (not mcp-ad-engine-path)
      (error "auto_dimension.lsp was not found in AutoCAD Support File Search Path")
    )
    (load mcp-ad-engine-path)
    (setq *mcp-auto-dimension-loader-version* mcp-ad-loader-target-version)
  )
)

(princ)
