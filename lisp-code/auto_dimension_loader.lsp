;;; Lightweight, version-aware loader for the automatic dimension engine.
;;; The dispatcher may load this tiny file for every annotation IPC request, but
;;; the planning, ActiveX commit, and scoped export engines are parsed only once
;;; per AutoCAD document/version.

(setq mcp-ad-loader-target-version "phase3-2026-07-19-loader-fix")

(defun mcp-ad-find-sibling (filename / sibling)
  (setq sibling
    (if (and (boundp 'mcp-ad-loader-path) mcp-ad-loader-path)
      (strcat (vl-filename-directory mcp-ad-loader-path) "/" filename)
      nil))
  (cond
    ((and sibling (findfile sibling)) sibling)
    ((findfile filename))
    (T nil)))
(if
  (or
    (not (boundp '*mcp-auto-dimension-loader-version*))
    (/= *mcp-auto-dimension-loader-version* mcp-ad-loader-target-version)
  )
  (progn
    (setq mcp-ad-engine-path (mcp-ad-find-sibling "auto_dimension.lsp"))
    (if (not mcp-ad-engine-path)
      (error "auto_dimension.lsp was not found beside the loader or in AutoCAD Support File Search Path")
    )
    (setq mcp-ad-activex-path (mcp-ad-find-sibling "auto_dimension_activex.lsp"))
    (if (not mcp-ad-activex-path)
      (error "auto_dimension_activex.lsp was not found beside the loader or in AutoCAD Support File Search Path")
    )
    (setq mcp-ad-scope-path (mcp-ad-find-sibling "auto_dimension_scope.lsp"))
    (if (not mcp-ad-scope-path)
      (error "auto_dimension_scope.lsp was not found beside the loader or in AutoCAD Support File Search Path")
    )
    (load mcp-ad-engine-path)
    ;; Loaded second so it replaces only the final mutation/commit entry point.
    (load mcp-ad-activex-path)
    ;; Loaded last so it replaces only the read-only geometry exporter.
    (load mcp-ad-scope-path)
    (setq *mcp-auto-dimension-loader-version* mcp-ad-loader-target-version)
  )
)
(princ)
