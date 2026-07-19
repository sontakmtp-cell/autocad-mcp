;;; Phase 3 scoped geometry exporter.
;;; Loaded after auto_dimension.lsp and overrides only the read-only exporter.
;;; Selector controls are transported inside the existing source_layers field so
;;; the deployed IPC dispatcher remains backward compatible.

(setq mcp-ad-scope-handle-prefix "__MCP_SCOPE_HANDLE__:")
(setq mcp-ad-scope-region-prefix "__MCP_SCOPE_REGION__:")

(defun mcp-ad-scope-prefix-p (text prefix)
  (and text
       (>= (strlen text) (strlen prefix))
       (= (substr text 1 (strlen prefix)) prefix))
)

(defun mcp-ad-scope-split (text delimiter / position result delimiter-length)
  (setq result '() delimiter-length (strlen delimiter))
  (while (setq position (vl-string-search delimiter text))
    (setq result (cons (substr text 1 position) result))
    (setq text (substr text (+ position delimiter-length 1)))
  )
  (reverse (cons text result))
)

(defun mcp-ad-scope-parse
  (source-layers / token handles layers region parts mode x1 y1 x2 y2)
  (setq handles '() layers '() region nil mode "intersect")
  (foreach token source-layers
    (cond
      ((mcp-ad-scope-prefix-p token mcp-ad-scope-handle-prefix)
        (setq handles
          (cons
            (substr token (+ (strlen mcp-ad-scope-handle-prefix) 1))
            handles))
      )
      ((mcp-ad-scope-prefix-p token mcp-ad-scope-region-prefix)
        (setq parts
          (mcp-ad-scope-split
            (substr token (+ (strlen mcp-ad-scope-region-prefix) 1))
            ","))
        (if (= (length parts) 5)
          (progn
            (setq mode (nth 0 parts)
                  x1 (atof (nth 1 parts))
                  y1 (atof (nth 2 parts))
                  x2 (atof (nth 3 parts))
                  y2 (atof (nth 4 parts)))
            (setq region
              (list
                (list (min x1 x2) (min y1 y2) 0.0)
                (list (max x1 x2) (max y1 y2) 0.0)))
          )
        )
      )
      (T (setq layers (cons token layers)))
    )
  )
  (list (reverse layers) (reverse handles) region mode)
)

(defun mcp-ad-scope-supported-entity-p (entity / data entity-type layout)
  (setq data (entget entity)
        entity-type (cdr (assoc 0 data))
        layout (cdr (assoc 410 data)))
  (and
    (= layout "Model")
    (member entity-type
      '("LINE" "LWPOLYLINE" "POLYLINE" "CIRCLE" "ARC"
        "ELLIPSE" "INSERT" "DIMENSION")))
)

(defun mcp-export-dimension-geometry
  (report-file dim-layer source-layers use-current-selection
   / started scope-data actual-layers handles region region-mode selection-scope
   selection filter entity handle missing-count scanned-count fp index data layer
   record first count elapsed)
  "Export only the requested Model Space scope and report scan telemetry."
  (setq started (getvar "MILLISECS"))
  (setq scope-data (mcp-ad-scope-parse source-layers)
        actual-layers (nth 0 scope-data)
        handles (nth 1 scope-data)
        region (nth 2 scope-data)
        region-mode (nth 3 scope-data)
        missing-count 0)
  (setq filter
    '((0 . "LINE,LWPOLYLINE,POLYLINE,CIRCLE,ARC,ELLIPSE,INSERT,DIMENSION")
      (410 . "Model")))
  (cond
    (use-current-selection
      (setq selection (ssget "_I" filter)
            selection-scope "current_selection"))
    (handles
      (setq selection (ssadd)
            selection-scope "entity_handles")
      (foreach handle handles
        (setq entity (handent handle))
        (if (and entity (mcp-ad-scope-supported-entity-p entity))
          (ssadd entity selection)
          (setq missing-count (1+ missing-count)))
      ))
    (region
      (setq selection
        (ssget
          (if (= region-mode "contained") "_W" "_C")
          (nth 0 region)
          (nth 1 region)
          filter))
      (setq selection-scope
        (if (= region-mode "contained") "region_window" "region_crossing")))
    (T
      (setq selection (ssget "_X" filter)
            selection-scope "modelspace"))
  )
  (setq scanned-count (if selection (sslength selection) 0))
  (setq fp (open report-file "w"))
  (if (not fp)
    nil
    (progn
      (write-line "{\"ok\":true,\"entities\":[" fp)
      (setq first T count 0 index 0)
      (if selection
        (while (< index (sslength selection))
          (setq entity (ssname selection index))
          (setq data (entget entity))
          (setq layer (cdr (assoc 8 data)))
          (if (mcp-ad-layer-allowed-p layer dim-layer actual-layers)
            (progn
              (setq record (mcp-ad-entity-json entity))
              (if record
                (progn
                  (if (not first) (write-line "," fp))
                  (princ record fp)
                  (setq first nil count (1+ count))
                )
              )
            )
          )
          (setq index (1+ index))
        )
      )
      (setq elapsed (- (getvar "MILLISECS") started))
      (if (< elapsed 0) (setq elapsed 0))
      (write-line
        (strcat
          "],\"count\":" (itoa count)
          ",\"export_metrics\":{\"selection_scope\":\""
          selection-scope
          "\",\"scanned_count\":" (itoa scanned-count)
          ",\"exported_count\":" (itoa count)
          ",\"missing_handle_count\":" (itoa missing-count)
          ",\"elapsed_ms\":" (itoa elapsed)
          "}}")
        fp)
      (close fp)
      T
    )
  )
)

(princ)
