;;; mcp_dispatch.lsp — File-based IPC dispatcher for AutoCAD MCP v3.1
;;;
;;; Protocol:
;;;   1. Python writes command JSON to C:/temp/autocad_mcp_cmd_{id}.json
;;;   2. Python posts one exact request through ActiveX/COM
;;;   3. This function reads cmd, dispatches via command map, writes result JSON
;;;   4. Python polls for C:/temp/autocad_mcp_result_{id}.json
;;;
;;; SECURITY: No raw eval — dispatcher uses a command whitelist/map.
;;; Compatible with AutoCAD LT 2024+.

;; Load dependencies
(if (not report-error)
  (defun report-error (msg) (princ (strcat "\nERROR: " msg)))
)

;; IPC directory
(setq *mcp-ipc-dir* "C:/temp/")

;; -----------------------------------------------------------------------
;; JSON-like output helpers (minimal, no external library)
;; -----------------------------------------------------------------------

(defun mcp-write-result (filepath request-id ok-flag payload error-msg / fp)
  "Write a result JSON file. Atomic: write to .tmp then rename."
  (setq tmp-path (strcat filepath ".tmp"))
  (setq fp (open tmp-path "w"))
  (if fp
    (progn
      (write-line "{" fp)
      (write-line (strcat "  \"request_id\": \"" request-id "\",") fp)
      (if ok-flag
        (progn
          (write-line "  \"ok\": true," fp)
          (write-line (strcat "  \"payload\": " payload) fp)
        )
        (progn
          (write-line "  \"ok\": false," fp)
          (write-line (strcat "  \"error\": \"" (mcp-escape-string error-msg) "\"") fp)
        )
      )
      (write-line "}" fp)
      (close fp)
      ;; Rename .tmp to final path (atomic on NTFS)
      (vl-file-rename tmp-path filepath)
    )
    (princ (strcat "\nMCP: Cannot open result file: " tmp-path))
  )
)

(defun mcp-escape-string (s / result i ch)
  "Escape quotes and backslashes in a string for JSON."
  (if (null s) (setq s ""))
  (setq result "" i 1)
  (while (<= i (strlen s))
    (setq ch (substr s i 1))
    (cond
      ((= ch "\"") (setq result (strcat result "\\\"")))
      ((= ch "\\") (setq result (strcat result "\\\\")))
      (t (setq result (strcat result ch)))
    )
    (setq i (1+ i))
  )
  result
)

(defun mcp-read-file-lines (filepath / fp line lines)
  "Read all lines from a file into a single string."
  (setq fp (open filepath "r"))
  (if (not fp) (progn (princ (strcat "\nMCP: Cannot read: " filepath)) nil)
    (progn
      (setq lines "")
      (while (setq line (read-line fp))
        (setq lines (strcat lines line))
      )
      (close fp)
      lines
    )
  )
)

;; -----------------------------------------------------------------------
;; Simple JSON parser (extracts string values by key)
;; -----------------------------------------------------------------------

(defun mcp-json-get-string (json key / search-str pos end-pos value)
  "Extract a string value for a given key from JSON text."
  (setq search-str (strcat "\"" key "\""))
  (setq pos (vl-string-search search-str json))
  (if (null pos) nil
    (progn
      ;; Find the colon after key
      (setq pos (vl-string-search ":" json pos))
      (if (null pos) nil
        (progn
          ;; Find opening quote of value
          (setq pos (vl-string-search "\"" json (1+ pos)))
          (if (null pos) nil
            (progn
              (setq pos (+ pos 2))  ; 0-based search result + 2 = 1-based position after quote
              ;; Find closing quote (skip escaped quotes)
              (setq end-pos pos)
              (while (and (<= end-pos (strlen json))
                          (or (= end-pos pos)
                              (/= (substr json end-pos 1) "\"")))
                ;; Handle escaped characters
                (if (= (substr json end-pos 1) "\\")
                  (setq end-pos (+ end-pos 2))
                  (setq end-pos (1+ end-pos))
                )
              )
              (substr json pos (- end-pos pos))
            )
          )
        )
      )
    )
  )
)

(defun mcp-json-get-number (json key / search-str pos num-start num-end ch)
  "Extract a number value for a given key from JSON text."
  (setq search-str (strcat "\"" key "\""))
  (setq pos (vl-string-search search-str json))
  (if (null pos) nil
    (progn
      (setq pos (vl-string-search ":" json pos))
      (if (null pos) nil
        (progn
          (setq pos (+ pos 2))  ; 0-based search result + 2 = 1-based position after colon
          ;; Skip whitespace
          (while (and (<= pos (strlen json))
                      (member (substr json pos 1) '(" " "\t" "\n")))
            (setq pos (1+ pos))
          )
          ;; Read number
          (setq num-start pos num-end pos)
          (while (and (<= num-end (strlen json))
                      (or (member (substr json num-end 1) '("0" "1" "2" "3" "4" "5" "6" "7" "8" "9" "." "-" "+"))
                      ))
            (setq num-end (1+ num-end))
          )
          (atof (substr json num-start (- num-end num-start)))
        )
      )
    )
  )
)

;; -----------------------------------------------------------------------
;; String splitting utility (used by semicolon-delimited encodings)
;; -----------------------------------------------------------------------

(defun mcp-split-string (str delim / pos result token)
  "Split a string by single-char delimiter. Returns a list of strings."
  (setq result '())
  (while (setq pos (vl-string-search delim str))
    (setq token (substr str 1 pos))
    (setq result (append result (list token)))
    (setq str (substr str (+ pos 2)))
  )
  (setq result (append result (list str)))
  result
)

;; -----------------------------------------------------------------------
;; Command dispatcher — WHITELIST ONLY, no eval
;; -----------------------------------------------------------------------

(defun mcp-dispatch-command-v31 (cmd-name params-json / result)
  "Dispatch a command by name. Returns (ok . payload-or-error)."
  (cond
    ;; --- Ping ---
    ((= cmd-name "ping")
     (cons T "\"pong\""))

    ;; --- Freehand LISP execution ---
    ((= cmd-name "execute-lisp")
     (mcp-cmd-execute-lisp params-json))

    ;; --- Undo / Redo ---
    ((= cmd-name "undo")
     (command "_.UNDO" "1") (cons T "\"undone\""))

    ((= cmd-name "redo")
     (command "_.REDO") (cons T "\"redone\""))

    ;; --- Drawing info ---
    ((= cmd-name "drawing-info")
     (mcp-cmd-drawing-info))

    ;; --- Layer operations ---
    ((= cmd-name "layer-list")
     (mcp-cmd-layer-list))

    ((= cmd-name "layer-create")
     (mcp-cmd-layer-create params-json))

    ((= cmd-name "layer-set-current")
     (mcp-cmd-layer-set-current params-json))

    ((= cmd-name "layer-set-properties")
     (mcp-cmd-layer-set-properties params-json))

    ((= cmd-name "layer-freeze")
     (mcp-cmd-layer-freeze params-json))

    ((= cmd-name "layer-thaw")
     (mcp-cmd-layer-thaw params-json))

    ((= cmd-name "layer-lock")
     (mcp-cmd-layer-lock params-json))

    ((= cmd-name "layer-unlock")
     (mcp-cmd-layer-unlock params-json))

    ;; --- Entity creation ---
    ((= cmd-name "create-line")
     (mcp-cmd-create-line params-json))

    ((= cmd-name "create-circle")
     (mcp-cmd-create-circle params-json))

    ((= cmd-name "create-polyline")
     (mcp-cmd-create-polyline params-json))

    ((= cmd-name "create-rectangle")
     (mcp-cmd-create-rectangle params-json))

    ((= cmd-name "create-text")
     (mcp-cmd-create-text params-json))

    ((= cmd-name "create-arc")
     (mcp-cmd-create-arc params-json))

    ((= cmd-name "create-ellipse")
     (mcp-cmd-create-ellipse params-json))

    ((= cmd-name "create-mtext")
     (mcp-cmd-create-mtext params-json))

    ((= cmd-name "create-hatch")
     (mcp-cmd-create-hatch params-json))

    ;; --- Entity queries ---
    ((= cmd-name "entity-count")
     (mcp-cmd-entity-count params-json))

    ((= cmd-name "entity-list")
     (mcp-cmd-entity-list params-json))

    ((= cmd-name "entity-get")
     (mcp-cmd-entity-get params-json))

    ((= cmd-name "entity-erase")
     (mcp-cmd-entity-erase params-json))

    ;; --- Entity modification ---
    ((= cmd-name "entity-move")
     (mcp-cmd-entity-move params-json))

    ((= cmd-name "entity-copy")
     (mcp-cmd-entity-copy params-json))

    ((= cmd-name "entity-rotate")
     (mcp-cmd-entity-rotate params-json))

    ((= cmd-name "entity-scale")
     (mcp-cmd-entity-scale params-json))

    ((= cmd-name "entity-mirror")
     (mcp-cmd-entity-mirror params-json))

    ((= cmd-name "entity-offset")
     (mcp-cmd-entity-offset params-json))

    ((= cmd-name "entity-array")
     (mcp-cmd-entity-array params-json))

    ((= cmd-name "entity-fillet")
     (mcp-cmd-entity-fillet params-json))

    ((= cmd-name "entity-chamfer")
     (mcp-cmd-entity-chamfer params-json))

    ;; --- View ---
    ((= cmd-name "zoom-extents")
     (command "_.ZOOM" "_E")
     (cons T "\"zoomed to extents\""))

    ((= cmd-name "zoom-window")
     (progn
       (setq x1 (mcp-json-get-number params-json "x1"))
       (setq y1 (mcp-json-get-number params-json "y1"))
       (setq x2 (mcp-json-get-number params-json "x2"))
       (setq y2 (mcp-json-get-number params-json "y2"))
       (command "_.ZOOM" "_W" (list x1 y1 0) (list x2 y2 0))
       (cons T "\"zoomed to window\"")))

    ;; --- Drawing file ops ---
    ((= cmd-name "drawing-save")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if (and path (> (strlen path) 0))
         (mcp-call-with-sysvars
           '(("FILEDIA" 0) ("CMDECHO" 0))
           'mcp-saveas-raw
           (list path))
         (progn (command "_.QSAVE") (cons T "\"saved\"")))))

    ((= cmd-name "drawing-save-as-dxf")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if path
         (mcp-call-with-sysvars
           '(("FILEDIA" 0) ("CMDECHO" 0))
           'mcp-save-dxf-raw
           (list path))
         (mcp-error "path_required" "Save path required"))))

    ((= cmd-name "drawing-purge")
     (command "_.-PURGE" "_ALL" "*" "_N")
     (cons T "\"purged\""))

    ((= cmd-name "drawing-open")
     (progn
       (setq path (mcp-json-get-string params-json "path"))
       (if path
         (mcp-call-with-sysvars
           '(("FILEDIA" 0) ("CMDECHO" 0))
           'mcp-open-raw
           (list path))
         (mcp-error "path_required" "Open path required"))))

    ;; --- P&ID ---
    ((= cmd-name "pid-setup-layers")
     (if c:setup-pid-layers
       (progn (c:setup-pid-layers) (cons T "\"P&ID layers created\""))
       (cons nil "pid_tools.lsp not loaded")))

    ((= cmd-name "pid-insert-symbol")
     (mcp-cmd-pid-insert-symbol params-json))

    ((= cmd-name "pid-draw-process-line")
     (mcp-cmd-pid-draw-process-line params-json))

    ((= cmd-name "pid-connect-equipment")
     (mcp-cmd-pid-connect-equipment params-json))

    ((= cmd-name "pid-add-flow-arrow")
     (mcp-cmd-pid-add-flow-arrow params-json))

    ((= cmd-name "pid-add-equipment-tag")
     (mcp-cmd-pid-add-equipment-tag params-json))

    ((= cmd-name "pid-add-line-number")
     (mcp-cmd-pid-add-line-number params-json))

    ((= cmd-name "pid-insert-valve")
     (mcp-cmd-pid-insert-valve params-json))

    ((= cmd-name "pid-insert-instrument")
     (mcp-cmd-pid-insert-instrument params-json))

    ((= cmd-name "pid-insert-pump")
     (mcp-cmd-pid-insert-pump params-json))

    ((= cmd-name "pid-insert-tank")
     (mcp-cmd-pid-insert-tank params-json))

    ;; --- Block operations ---
    ((= cmd-name "block-list")
     (mcp-cmd-block-list))

    ((= cmd-name "block-insert")
     (mcp-cmd-block-insert params-json))

    ((= cmd-name "block-insert-with-attributes")
     (mcp-cmd-block-insert-with-attribs params-json))

    ((= cmd-name "block-get-attributes")
     (mcp-cmd-block-get-attributes params-json))

    ((= cmd-name "block-update-attribute")
     (mcp-cmd-block-update-attribute params-json))

    ((= cmd-name "block-define")
     (cons nil "block-define not available via IPC (use ezdxf backend)"))

    ;; --- Annotation ---
    ((= cmd-name "create-dimension-linear")
     (mcp-cmd-create-dimension-linear params-json))

    ((= cmd-name "create-dimension-aligned")
     (mcp-cmd-create-dimension-aligned params-json))

    ((= cmd-name "create-dimension-angular")
     (mcp-cmd-create-dimension-angular params-json))

    ((= cmd-name "create-dimension-radius")
     (mcp-cmd-create-dimension-radius params-json))

    ((= cmd-name "create-leader")
     (mcp-cmd-create-leader params-json))

    ((= cmd-name "annotation-export-dimension-geometry")
     (mcp-cmd-annotation-export-dimension-geometry params-json))

    ((= cmd-name "annotation-commit-dimension-plan")
     (mcp-cmd-annotation-commit-dimension-plan params-json))

    ((= cmd-name "annotation-repair-dimensions")
     (mcp-cmd-annotation-repair-dimensions params-json))

    ;; --- Drawing management ---
    ((= cmd-name "drawing-create")
     (mcp-cmd-drawing-create params-json))

    ((= cmd-name "drawing-get-variables")
     (mcp-cmd-drawing-get-variables params-json))

    ((= cmd-name "drawing-plot-pdf")
     (mcp-cmd-drawing-plot-pdf params-json))

    ;; --- P&ID list symbols ---
    ((= cmd-name "pid-list-symbols")
     (mcp-cmd-pid-list-symbols params-json))

    ;; --- Unknown ---
    (t (cons nil (strcat "Unknown command: " cmd-name)))
  )
)

;; -----------------------------------------------------------------------
;; Command implementations
;; -----------------------------------------------------------------------

(defun mcp-cmd-drawing-info ( / count layers layer-list)
  "Return drawing info: entity count, layers, extents."
  (setq count 0)
  (setq ent (entnext))
  (while ent
    (setq count (1+ count))
    (setq ent (entnext ent))
  )
  (setq layer-list "")
  (setq layers (tblnext "LAYER" T))
  (while layers
    (if (> (strlen layer-list) 0)
      (setq layer-list (strcat layer-list ",\"" (cdr (assoc 2 layers)) "\""))
      (setq layer-list (strcat "\"" (cdr (assoc 2 layers)) "\""))
    )
    (setq layers (tblnext "LAYER"))
  )
  (cons T (strcat "{\"entity_count\":" (itoa count) ",\"layers\":[" layer-list "]}"))
)

(defun mcp-cmd-layer-list ( / layers layer-list name)
  "Return all layers as JSON array."
  (setq layer-list "")
  (setq layers (tblnext "LAYER" T))
  (while layers
    (setq name (cdr (assoc 2 layers)))
    (if (> (strlen layer-list) 0)
      (setq layer-list (strcat layer-list ",{\"name\":\"" name "\",\"color\":" (itoa (cdr (assoc 62 layers))) "}"))
      (setq layer-list (strcat "{\"name\":\"" name "\",\"color\":" (itoa (cdr (assoc 62 layers))) "}"))
    )
    (setq layers (tblnext "LAYER"))
  )
  (cons T (strcat "{\"layers\":[" layer-list "]}"))
)

(defun mcp-cmd-layer-create (params / name color linetype)
  (setq name (mcp-json-get-string params "name"))
  (setq color (mcp-json-get-string params "color"))
  (setq linetype (mcp-json-get-string params "linetype"))
  (if (not color) (setq color "white"))
  (if (not linetype) (setq linetype "CONTINUOUS"))
  (ensure_layer_exists name color linetype)
  (cons T (strcat "{\"name\":\"" name "\"}"))
)

(defun mcp-cmd-layer-set-current (params / name)
  (setq name (mcp-json-get-string params "name"))
  (setvar "CLAYER" name)
  (cons T (strcat "{\"current_layer\":\"" name "\"}"))
)

(defun mcp-cmd-create-line (params / x1 y1 x2 y2 layer)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_LINE" (list x1 y1 0.0) (list x2 y2 0.0) "")
  (cons T (strcat "{\"entity_type\":\"LINE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-circle (params / cx cy radius layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_CIRCLE" (list cx cy 0.0) radius)
  (cons T (strcat "{\"entity_type\":\"CIRCLE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-polyline (params / pts-str closed layer pairs pt-str cx cy)
  (setq pts-str (mcp-json-get-string params "points_str"))
  (setq closed (mcp-json-get-string params "closed"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (command "_PLINE")
      (setq pairs (mcp-split-string pts-str ";"))
      (foreach pt-str pairs
        (setq cx (atof (car (mcp-split-string pt-str ","))))
        (setq cy (atof (cadr (mcp-split-string pt-str ","))))
        (command (list cx cy 0.0))
      )
      (if (= closed "1") (command "_C") (command ""))
      (cons T (strcat "{\"entity_type\":\"LWPOLYLINE\",\"handle\":\""
                      (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
  )
)

(defun mcp-cmd-create-rectangle (params / x1 y1 x2 y2 layer)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_RECTANG" (list x1 y1 0.0) (list x2 y2 0.0))
  (cons T (strcat "{\"entity_type\":\"LWPOLYLINE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-text (params / x y text height rotation layer)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq text (mcp-json-get-string params "text"))
  (setq height (mcp-json-get-number params "height"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not height) (setq height 2.5))
  (if (not rotation) (setq rotation 0.0))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer
    (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer))
  )
  (command "_TEXT" "J" "M" (list x y 0.0) height rotation text)
  (cons T (strcat "{\"entity_type\":\"TEXT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-entity-count (params / layer count ent ent-data)
  (setq layer (mcp-json-get-string params "layer"))
  (setq count 0 ent (entnext))
  (while ent
    (setq ent-data (entget ent))
    (if (or (not layer) (= (cdr (assoc 8 ent-data)) layer))
      (setq count (1+ count))
    )
    (setq ent (entnext ent))
  )
  (cons T (strcat "{\"count\":" (itoa count) "}"))
)

(defun mcp-cmd-entity-list (params / layer entities ent ent-data etype handle elayer)
  (setq layer (mcp-json-get-string params "layer"))
  (setq entities "" ent (entnext))
  (while ent
    (setq ent-data (entget ent))
    (setq etype (cdr (assoc 0 ent-data)))
    (setq handle (cdr (assoc 5 ent-data)))
    (setq elayer (cdr (assoc 8 ent-data)))
    (if (or (not layer) (= elayer layer))
      (progn
        (if (> (strlen entities) 0)
          (setq entities (strcat entities ","))
        )
        (setq entities (strcat entities "{\"type\":\"" etype "\",\"handle\":\"" handle "\",\"layer\":\"" elayer "\"}"))
      )
    )
    (setq ent (entnext ent))
  )
  (cons T (strcat "{\"entities\":[" entities "]}"))
)

(defun mcp-cmd-entity-erase (params / entity-id ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last")
    (progn
      (setq ent (entlast))
      (if ent (progn (entdel ent) (cons T "\"erased last entity\""))
        (cons nil "No entity to erase")))
    (progn
      (setq ent (handent entity-id))
      (if ent (progn (entdel ent) (cons T (strcat "\"erased " entity-id "\"")))
        (cons nil (strcat "Entity not found: " entity-id))))
  )
)

(defun mcp-cmd-entity-move (params / entity-id dx dy ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq dx (mcp-json-get-number params "dx"))
  (setq dy (mcp-json-get-number params "dy"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if ent
    (progn
      (command "_.MOVE" ent "" '(0 0 0) (list dx dy 0))
      (cons T "\"moved\""))
    (cons nil "Entity not found")
  )
)

;; --- Freehand LISP execution ---

(defun mcp-cmd-execute-lisp (params / code-file)
  (setq code-file (mcp-json-get-string params "code_file"))
  (if (not code-file)
    (cons nil "code_file parameter required")
    (if (not (findfile code-file))
      (cons nil (strcat "Code file not found: " code-file))
      (mcp-call-with-sysvars
        '(("SECURELOAD" 0))
        'mcp-load-code-file-raw
        (list code-file))
    )
  )
)

;; --- Drawing create implementation ---

(defun mcp-cmd-drawing-create (params / ss)
  "Reset current drawing to a clean state (erase all, purge, reset to layer 0).
   Using _.NEW would create a new document tab with a fresh LISP namespace,
   breaking the IPC dispatcher. This approach preserves the dispatcher."
  (if (setq ss (ssget "_X"))
    (progn (command "_.ERASE" ss "") (setq ss nil))
  )
  (setvar "CLAYER" "0")
  (command "_.-PURGE" "_ALL" "*" "_N")
  (cons T (strcat "{\"drawing\":\"" (mcp-escape-string (getvar "DWGNAME")) "\"}"))
)

;; --- P&ID command implementations ---

(defun mcp-cmd-pid-insert-symbol (params / category symbol x y scale rotation)
  (setq category (mcp-json-get-string params "category"))
  (setq symbol (mcp-json-get-string params "symbol"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-pid-block
    (progn
      (c:insert-pid-block category symbol x y scale rotation)
      (cons T (strcat "{\"symbol\":\"" symbol "\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-draw-process-line (params / x1 y1 x2 y2)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if c:draw-process-line
    (progn (c:draw-process-line x1 y1 x2 y2) (cons T "\"process line drawn\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-connect-equipment (params / x1 y1 x2 y2)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if c:connect-equipment
    (progn (c:connect-equipment x1 y1 x2 y2) (cons T "\"equipment connected\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-flow-arrow (params / x y rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:add-flow-arrow
    (progn (c:add-flow-arrow x y rotation) (cons T "\"flow arrow added\""))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-equipment-tag (params / x y tag description)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq tag (mcp-json-get-string params "tag"))
  (setq description (mcp-json-get-string params "description"))
  (if (not description) (setq description ""))
  (if c:add-equipment-tag
    (progn (c:add-equipment-tag x y tag description) (cons T (strcat "\"tagged: " tag "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-add-line-number (params / x y line-num spec)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq line-num (mcp-json-get-string params "line_num"))
  (setq spec (mcp-json-get-string params "spec"))
  (if c:add-line-number
    (progn (c:add-line-number x y line-num spec) (cons T (strcat "\"line number: " line-num "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-valve (params / x y valve-type rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq valve-type (mcp-json-get-string params "valve_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-valve-on-line
    (progn (c:insert-valve-on-line x y valve-type rotation) (cons T (strcat "\"valve: " valve-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-instrument (params / x y inst-type rotation tag-id range-value)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq inst-type (mcp-json-get-string params "instrument_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (setq tag-id (mcp-json-get-string params "tag_id"))
  (setq range-value (mcp-json-get-string params "range_value"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-instrument
    (progn
      (c:insert-instrument x y inst-type rotation)
      (if (and tag-id (> (strlen tag-id) 0))
        (c:insert-instrument-with-tag x y inst-type tag-id (if range-value range-value ""))
      )
      (cons T (strcat "\"instrument: " inst-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-pump (params / x y pump-type rotation)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq pump-type (mcp-json-get-string params "pump_type"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not rotation) (setq rotation 0.0))
  (if c:insert-pump
    (progn (c:insert-pump x y pump-type rotation) (cons T (strcat "\"pump: " pump-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

(defun mcp-cmd-pid-insert-tank (params / x y tank-type scale)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq tank-type (mcp-json-get-string params "tank_type"))
  (setq scale (mcp-json-get-number params "scale"))
  (if (not scale) (setq scale 1.0))
  (if c:insert-tank
    (progn (c:insert-tank x y tank-type scale) (cons T (strcat "\"tank: " tank-type "\"")))
    (cons nil "pid_tools.lsp not loaded")
  )
)

;; --- Additional entity creation ---

(defun mcp-cmd-create-arc (params / cx cy radius sa ea layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq sa (mcp-json-get-number params "start_angle"))
  (setq ea (mcp-json-get-number params "end_angle"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_ARC" "_C" (list cx cy 0.0) (list (+ cx radius) cy 0.0) "_A" (- ea sa))
  (cons T (strcat "{\"entity_type\":\"ARC\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-ellipse (params / cx cy mx my ratio layer)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq mx (mcp-json-get-number params "major_x"))
  (setq my (mcp-json-get-number params "major_y"))
  (setq ratio (mcp-json-get-number params "ratio"))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_ELLIPSE" "_C" (list cx cy 0.0) (list mx my 0.0) ratio)
  (cons T (strcat "{\"entity_type\":\"ELLIPSE\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-mtext (params / x y width text height layer)
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq width (mcp-json-get-number params "width"))
  (setq text (mcp-json-get-string params "text"))
  (setq height (mcp-json-get-number params "height"))
  (if (not height) (setq height 2.5))
  (setq layer (mcp-json-get-string params "layer"))
  (if layer (progn (ensure_layer_exists layer "white" "CONTINUOUS") (set_current_layer layer)))
  (command "_MTEXT" (list x y 0.0) "_H" height "_W" width text "")
  (cons T (strcat "{\"entity_type\":\"MTEXT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
)

(defun mcp-cmd-create-hatch (params / entity-id pattern ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq pattern (mcp-json-get-string params "pattern"))
  (if (not pattern) (setq pattern "ANSI31"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if ent
    (progn
      (command "_HATCH" "_P" pattern "" "_S" ent "" "")
      (cons T (strcat "{\"entity_type\":\"HATCH\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}")))
    (cons nil "Entity not found for hatching")
  )
)

;; --- Entity query: get ---

(defun mcp-cmd-entity-get (params / entity-id ent ent-data etype handle elayer result)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last")
    (setq ent (entlast))
    (setq ent (handent entity-id))
  )
  (if (not ent)
    (cons nil (strcat "Entity not found: " entity-id))
    (progn
      (setq ent-data (entget ent))
      (setq etype (cdr (assoc 0 ent-data)))
      (setq handle (cdr (assoc 5 ent-data)))
      (setq elayer (cdr (assoc 8 ent-data)))
      (setq result (strcat "{\"type\":\"" etype "\",\"handle\":\"" handle "\",\"layer\":\"" elayer "\""))
      ;; Add type-specific info
      (cond
        ((= etype "LINE")
         (setq result (strcat result
           ",\"start\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"end\":[" (rtos (car (cdr (assoc 11 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 11 ent-data))) 2 6) "]")))
        ((= etype "CIRCLE")
         (setq result (strcat result
           ",\"center\":[" (rtos (car (cdr (assoc 10 ent-data))) 2 6) "," (rtos (cadr (cdr (assoc 10 ent-data))) 2 6) "]"
           ",\"radius\":" (rtos (cdr (assoc 40 ent-data)) 2 6))))
      )
      (setq result (strcat result "}"))
      (cons T result)
    )
  )
)

;; --- Dedicated annotation workflow commands ---

(defun mcp-cmd-annotation-export-dimension-geometry
  (params / lisp-path report-path dimension-layer source-text source-layers
   current-text use-current-selection loaded result)
  (setq lisp-path (mcp-json-get-string params "lisp_path"))
  (setq report-path (mcp-json-get-string params "report_path"))
  (setq dimension-layer (mcp-json-get-string params "dimension_layer"))
  (setq source-text (mcp-json-get-string params "source_layers"))
  (setq current-text (mcp-json-get-string params "use_current_selection"))
  (setq use-current-selection (= current-text "1"))
  (setq source-layers
    (if (and source-text (> (strlen source-text) 0))
      (mcp-split-string source-text ";")
      nil))
  (setq mcp-ad-loader-path lisp-path)
  (setq loaded (vl-catch-all-apply 'load (list lisp-path)))
  (if (vl-catch-all-error-p loaded)
    (mcp-error "annotation_engine_load_failed" (vl-catch-all-error-message loaded))
    (progn
      (setq result
        (vl-catch-all-apply
          'mcp-export-dimension-geometry
          (list report-path dimension-layer source-layers use-current-selection)))
      (if (or (vl-catch-all-error-p result) (not result))
        (mcp-error "annotation_export_failed"
          (if (vl-catch-all-error-p result)
            (vl-catch-all-error-message result)
            "Geometry export did not produce a report."))
        (cons T "{\"exported\":true}"))
    ))
)

(defun mcp-cmd-annotation-commit-dimension-plan
  (params / lisp-path plan-path report-path dimension-layer clear-text clear-existing
   dimstyle scale-factor text-height arrow-size precision tolerance-mode tolerance-upper
   tolerance-lower loaded result)
  (setq lisp-path (mcp-json-get-string params "lisp_path"))
  (setq plan-path (mcp-json-get-string params "plan_path"))
  (setq report-path (mcp-json-get-string params "report_path"))
  (setq dimension-layer (mcp-json-get-string params "dimension_layer"))
  (setq dimstyle (mcp-json-get-string params "dimstyle"))
  (setq scale-factor (mcp-json-get-number params "scale_factor"))
  (setq clear-text (mcp-json-get-string params "clear_existing"))
  (setq clear-existing (= clear-text "1"))
  (setq text-height (mcp-json-get-number params "text_height"))
  (setq arrow-size (mcp-json-get-number params "arrow_size"))
  (setq precision (fix (mcp-json-get-number params "precision")))
  (setq tolerance-mode (mcp-json-get-string params "tolerance_mode"))
  (setq tolerance-upper (mcp-json-get-number params "tolerance_upper"))
  (setq tolerance-lower (mcp-json-get-number params "tolerance_lower"))
  (setq mcp-ad-loader-path lisp-path)
  (setq loaded (vl-catch-all-apply 'load (list lisp-path)))
  (if (vl-catch-all-error-p loaded)
    (mcp-error "annotation_engine_load_failed" (vl-catch-all-error-message loaded))
    (progn
      (setq result
        (vl-catch-all-apply
          'mcp-commit-dimension-plan-file
          (list plan-path report-path dimension-layer clear-existing
                dimstyle scale-factor text-height arrow-size precision tolerance-mode
                tolerance-upper tolerance-lower)))
      (if (or (vl-catch-all-error-p result) (not result))
        (mcp-error "annotation_commit_failed"
          (if (vl-catch-all-error-p result)
            (vl-catch-all-error-message result)
            "Dimension plan commit failed."))
        (cons T "{\"committed\":true,\"undo_group\":\"single\"}"))
    ))
)

(defun mcp-cmd-annotation-repair-dimensions
  (params / lisp-path actions-path report-path dimension-layer dimstyle loaded result)
  (setq lisp-path (mcp-json-get-string params "lisp_path"))
  (setq actions-path (mcp-json-get-string params "actions_path"))
  (setq report-path (mcp-json-get-string params "report_path"))
  (setq dimension-layer (mcp-json-get-string params "dimension_layer"))
  (setq dimstyle (mcp-json-get-string params "dimstyle"))
  (setq mcp-ad-loader-path lisp-path)
  (setq loaded (vl-catch-all-apply 'load (list lisp-path)))
  (if (vl-catch-all-error-p loaded)
    (mcp-error "annotation_engine_load_failed" (vl-catch-all-error-message loaded))
    (progn
      (setq result
        (vl-catch-all-apply
          'mcp-repair-dimensions-file
          (list actions-path report-path dimension-layer dimstyle)))
      (if (or (vl-catch-all-error-p result) (not result))
        (mcp-error "annotation_repair_failed"
          (if (vl-catch-all-error-p result)
            (vl-catch-all-error-message result)
            "Dimension repair failed."))
        (cons T "{\"repaired\":true,\"undo_group\":\"single\"}"))
    ))
)

;; --- Entity modification commands ---

(defun mcp-cmd-entity-copy (params / entity-id dx dy ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq dx (mcp-json-get-number params "dx"))
  (setq dy (mcp-json-get-number params "dy"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.COPY" ent "" '(0 0 0) (list dx dy 0))
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-rotate (params / entity-id cx cy angle ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq angle (mcp-json-get-number params "angle"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn (command "_.ROTATE" ent "" (list cx cy 0) angle) (cons T "\"rotated\""))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-scale (params / entity-id cx cy factor ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq factor (mcp-json-get-number params "factor"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn (command "_.SCALE" ent "" (list cx cy 0) factor) (cons T "\"scaled\""))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-mirror (params / entity-id x1 y1 x2 y2 ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.MIRROR" ent "" (list x1 y1 0) (list x2 y2 0) "_N")
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-offset (params / entity-id distance ent new-handle)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq distance (mcp-json-get-number params "distance"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.OFFSET" distance ent (list 0 0 0) "")
      (setq new-handle (cdr (assoc 5 (entget (entlast)))))
      (cons T (strcat "{\"handle\":\"" new-handle "\"}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-array (params / entity-id rows cols row-dist col-dist ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq rows (fix (mcp-json-get-number params "rows")))
  (setq cols (fix (mcp-json-get-number params "cols")))
  (setq row-dist (mcp-json-get-number params "row_dist"))
  (setq col-dist (mcp-json-get-number params "col_dist"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if ent
    (progn
      (command "_.ARRAY" ent "" "_R" rows cols row-dist col-dist)
      (cons T (strcat "{\"rows\":" (itoa rows) ",\"cols\":" (itoa cols) "}")))
    (cons nil "Entity not found")
  )
)

(defun mcp-cmd-entity-fillet (params / id1 id2 radius ent1 ent2)
  (setq id1 (mcp-json-get-string params "id1"))
  (setq id2 (mcp-json-get-string params "id2"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq ent1 (handent id1))
  (setq ent2 (handent id2))
  (if (and ent1 ent2)
    (progn
      (command "_.FILLET" "_R" radius)
      (command "_.FILLET" ent1 ent2)
      (cons T "\"filleted\""))
    (cons nil "One or both entities not found")
  )
)

(defun mcp-cmd-entity-chamfer (params / id1 id2 dist1 dist2 ent1 ent2)
  (setq id1 (mcp-json-get-string params "id1"))
  (setq id2 (mcp-json-get-string params "id2"))
  (setq dist1 (mcp-json-get-number params "dist1"))
  (setq dist2 (mcp-json-get-number params "dist2"))
  (setq ent1 (handent id1))
  (setq ent2 (handent id2))
  (if (and ent1 ent2)
    (progn
      (command "_.CHAMFER" "_D" dist1 dist2)
      (command "_.CHAMFER" ent1 ent2)
      (cons T "\"chamfered\""))
    (cons nil "One or both entities not found")
  )
)

;; --- Layer operations ---

(defun mcp-cmd-layer-set-properties (params / name color linetype lineweight)
  (setq name (mcp-json-get-string params "name"))
  (setq color (mcp-json-get-string params "color"))
  (setq linetype (mcp-json-get-string params "linetype"))
  (setq lineweight (mcp-json-get-string params "lineweight"))
  (if color (command "_.-LAYER" "_COLOR" color name ""))
  (if linetype (command "_.-LAYER" "_LTYPE" linetype name ""))
  (if lineweight (command "_.-LAYER" "_LWEIGHT" lineweight name ""))
  (cons T (strcat "{\"name\":\"" name "\"}"))
)

(defun mcp-cmd-layer-freeze (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_FREEZE" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"frozen\":true}"))
)

(defun mcp-cmd-layer-thaw (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_THAW" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"frozen\":false}"))
)

(defun mcp-cmd-layer-lock (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_LOCK" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"locked\":true}"))
)

(defun mcp-cmd-layer-unlock (params / name)
  (setq name (mcp-json-get-string params "name"))
  (command "_.-LAYER" "_UNLOCK" name "")
  (cons T (strcat "{\"name\":\"" name "\",\"locked\":false}"))
)

;; --- Block operations (insert-with-attributes, get-attributes, update-attribute) ---

(defun mcp-cmd-block-insert-with-attribs (params / name x y scale rotation attributes ent)
  (setq name (mcp-json-get-string params "name"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if (tblsearch "BLOCK" name)
    (progn
      ;; Insert with ATTREQ=1 to fill attributes
      (command "_.INSERT" name (list x y 0.0) scale scale rotation)
      ;; Note: attribute values are applied separately via update-attribute
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}")))
    (cons nil (strcat "Block '" name "' not found"))
  )
)

(defun mcp-cmd-block-get-attributes (params / entity-id ent sub-ent ent-data attribs)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if (not ent)
    (cons nil "Entity not found")
    (progn
      (setq attribs "" sub-ent (entnext ent))
      (while sub-ent
        (setq ent-data (entget sub-ent))
        (if (= (cdr (assoc 0 ent-data)) "ATTRIB")
          (progn
            (if (> (strlen attribs) 0) (setq attribs (strcat attribs ",")))
            (setq attribs (strcat attribs "\"" (cdr (assoc 2 ent-data)) "\":\"" (mcp-escape-string (cdr (assoc 1 ent-data))) "\""))
          )
        )
        (if (= (cdr (assoc 0 ent-data)) "SEQEND")
          (setq sub-ent nil)
          (setq sub-ent (entnext sub-ent))
        )
      )
      (cons T (strcat "{\"attributes\":{" attribs "}}"))
    )
  )
)

(defun mcp-cmd-block-update-attribute (params / entity-id tag value ent)
  (setq entity-id (mcp-json-get-string params "entity_id"))
  (setq tag (mcp-json-get-string params "tag"))
  (setq value (mcp-json-get-string params "value"))
  (if (= entity-id "last") (setq ent (entlast)) (setq ent (handent entity-id)))
  (if (not ent)
    (cons nil "Entity not found")
    (progn
      (if c:update-block-attribute
        (progn (c:update-block-attribute ent tag value)
               (cons T (strcat "{\"tag\":\"" tag "\",\"value\":\"" (mcp-escape-string value) "\"}")))
        ;; Inline fallback if attribute_tools.lsp not loaded
        (progn
          (set_attribute_value ent tag value)
          (cons T (strcat "{\"tag\":\"" tag "\",\"value\":\"" (mcp-escape-string value) "\"}")))
      )
    )
  )
)

;; --- Annotation commands ---

(defun mcp-cmd-create-dimension-linear (params / x1 y1 x2 y2 dim-x dim-y)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq dim-x (mcp-json-get-number params "dim_x"))
  (setq dim-y (mcp-json-get-number params "dim_y"))
  (command "_.DIMLINEAR" (list x1 y1 0) (list x2 y2 0) (list dim-x dim-y 0))
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-dimension-aligned (params / x1 y1 x2 y2 offset)
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (setq offset (mcp-json-get-number params "offset"))
  ;; Place dimension line at offset distance
  (command "_.DIMALIGNED" (list x1 y1 0) (list x2 y2 0)
    (list (+ (/ (+ x1 x2) 2.0) offset) (+ (/ (+ y1 y2) 2.0) offset) 0))
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-dimension-angular (params / cx cy x1 y1 x2 y2)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq x1 (mcp-json-get-number params "x1"))
  (setq y1 (mcp-json-get-number params "y1"))
  (setq x2 (mcp-json-get-number params "x2"))
  (setq y2 (mcp-json-get-number params "y2"))
  (command "_.DIMANGULAR" (list cx cy 0) (list x1 y1 0) (list x2 y2 0) "")
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-dimension-radius (params / cx cy radius angle px py)
  (setq cx (mcp-json-get-number params "cx"))
  (setq cy (mcp-json-get-number params "cy"))
  (setq radius (mcp-json-get-number params "radius"))
  (setq angle (mcp-json-get-number params "angle"))
  ;; Need a circle/arc entity first, use entity at center
  (setq px (+ cx (* radius (cos (* angle (/ pi 180.0))))))
  (setq py (+ cy (* radius (sin (* angle (/ pi 180.0))))))
  (command "_.DIMRADIUS" (list px py 0) "")
  (cons T "{\"entity_type\":\"DIMENSION\"}")
)

(defun mcp-cmd-create-leader (params / text pts-str pairs pt-str)
  (setq text (mcp-json-get-string params "text"))
  (setq pts-str (mcp-json-get-string params "points_str"))
  (if (not pts-str)
    (cons nil "points_str required (format: x1,y1;x2,y2;...)")
    (progn
      (command "_.LEADER")
      (setq pairs (mcp-split-string pts-str ";"))
      (foreach pt-str pairs
        (command (list (atof (car (mcp-split-string pt-str ",")))
                       (atof (cadr (mcp-split-string pt-str ","))) 0))
      )
      (command "" text "")
      (cons T "{\"entity_type\":\"LEADER\"}")
    )
  )
)

;; --- Drawing management ---

(defun mcp-cmd-drawing-get-variables (params / names-str result var-list var-name var-val first-var)
  (setq names-str (mcp-json-get-string params "names_str"))
  (if (or (not names-str) (= names-str ""))
    ;; Default set when no specific names requested
    (progn
      (setq result "{")
      (setq result (strcat result "\"ACADVER\":\"" (getvar "ACADVER") "\""))
      (setq result (strcat result ",\"DWGNAME\":\"" (mcp-escape-string (getvar "DWGNAME")) "\""))
      (setq result (strcat result ",\"CLAYER\":\"" (getvar "CLAYER") "\""))
      (setq result (strcat result "}"))
      (cons T result)
    )
    ;; Parse semicolon-delimited variable names
    (progn
      (setq var-list (mcp-split-string names-str ";"))
      (setq result "{" first-var T)
      (foreach var-name var-list
        (setq var-val (getvar var-name))
        (if (not first-var) (setq result (strcat result ",")))
        (setq first-var nil)
        (if (not var-val)
          (setq result (strcat result "\"" var-name "\":null"))
          (cond
            ((= (type var-val) 'STR)
             (setq result (strcat result "\"" var-name "\":\"" (mcp-escape-string var-val) "\"")))
            ((= (type var-val) 'INT)
             (setq result (strcat result "\"" var-name "\":" (itoa var-val))))
            ((= (type var-val) 'REAL)
             (setq result (strcat result "\"" var-name "\":" (rtos var-val 2 6))))
            (t
             (setq result (strcat result "\"" var-name "\":\"" (mcp-escape-string (vl-princ-to-string var-val)) "\"")))
          )
        )
      )
      (setq result (strcat result "}"))
      (cons T result)
    )
  )
)

(defun mcp-cmd-drawing-plot-pdf (params / path)
  (setq path (mcp-json-get-string params "path"))
  (if path
    (progn
      (command "_.-PLOT" "_Y" "" "DWG To PDF.pc3"
        "ANSI_A_(8.50_x_11.00_Inches)" "_Inches" "_Landscape"
        "_N" "_Extents" "_Fit" "_Y" "acad.ctb" "_Y" "_N" "_Y" path "_Y")
      (cons T (strcat "{\"path\":\"" (mcp-escape-string path) "\"}")))
    (cons nil "Plot path required")
  )
)

;; --- P&ID list symbols ---

(defun mcp-cmd-pid-list-symbols (params / category dir-path files result)
  (setq category (mcp-json-get-string params "category"))
  (setq dir-path (strcat "C:/PIDv4-CTO/" category "/"))
  (setq files (vl-directory-files dir-path "*.dwg" 1))
  (setq result "")
  (if files
    (foreach f files
      (if (> (strlen result) 0) (setq result (strcat result ",")))
      ;; Remove .dwg extension
      (setq result (strcat result "\"" (substr f 1 (- (strlen f) 4)) "\""))
    )
  )
  (cons T (strcat "{\"category\":\"" category "\",\"symbols\":[" result "],\"count\":" (itoa (length (if files files '()))) "}"))
)

;; --- Block operations ---

(defun mcp-cmd-block-list ( / blk block-list)
  (setq block-list "" blk (tblnext "BLOCK" T))
  (while blk
    (if (not (= (substr (cdr (assoc 2 blk)) 1 1) "*"))
      (progn
        (if (> (strlen block-list) 0)
          (setq block-list (strcat block-list ",\"" (cdr (assoc 2 blk)) "\""))
          (setq block-list (strcat "\"" (cdr (assoc 2 blk)) "\""))
        )
      )
    )
    (setq blk (tblnext "BLOCK"))
  )
  (cons T (strcat "{\"blocks\":[" block-list "]}"))
)

(defun mcp-cmd-block-insert (params / name x y scale rotation block-id)
  (setq name (mcp-json-get-string params "name"))
  (setq x (mcp-json-get-number params "x"))
  (setq y (mcp-json-get-number params "y"))
  (setq scale (mcp-json-get-number params "scale"))
  (setq rotation (mcp-json-get-number params "rotation"))
  (setq block-id (mcp-json-get-string params "block_id"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if (tblsearch "BLOCK" name)
    (progn
      (command "_.INSERT" name (list x y 0.0) scale scale rotation)
      (if (and block-id (> (strlen block-id) 0))
        (set_attribute_value (entlast) "ID" block-id)
      )
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\"" (cdr (assoc 5 (entget (entlast)))) "\"}"))
    )
    (cons nil (strcat "Block '" name "' not found"))
  )
)

;; -----------------------------------------------------------------------
;; Main dispatcher — called by "(c:mcp-dispatch)" from Python
;; -----------------------------------------------------------------------

(defun c:mcp-dispatch ( / cmd-files cmd-file json-text request-id cmd-name params-str result result-file)
  "Find pending command file, dispatch, write result."
  ;; Find first pending command file
  (setq cmd-files (vl-directory-files *mcp-ipc-dir* "autocad_mcp_cmd_*.json" 1))
  (if (not cmd-files)
    (progn (princ "\nMCP: No pending commands") (princ))
    (progn
      ;; Process first command
      (setq cmd-file (strcat *mcp-ipc-dir* (car cmd-files)))
      (setq json-text (mcp-read-file-lines cmd-file))

      (if (not json-text)
        (princ "\nMCP: Cannot read command file")
        (progn
          ;; Parse command
          (setq request-id (mcp-json-get-string json-text "request_id"))
          (setq cmd-name (mcp-json-get-string json-text "command"))

          (if (not cmd-name)
            (princ "\nMCP: No command in payload")
            (progn
              (princ (strcat "\nMCP: Dispatching " cmd-name " [" request-id "]"))

              ;; Execute via whitelist dispatcher
              (setq result
                (vl-catch-all-apply
                  'mcp-dispatch-command
                  (list cmd-name json-text)
                )
              )

              ;; Handle error from vl-catch-all-apply
              (if (vl-catch-all-error-p result)
                (setq result (cons nil (vl-catch-all-error-message result)))
              )

              ;; Write result
              (setq result-file (strcat *mcp-ipc-dir* "autocad_mcp_result_" request-id ".json"))
              (if (car result)
                (mcp-write-result result-file request-id T (cdr result) nil)
                (mcp-write-result result-file request-id nil nil (cdr result))
              )

              (princ (strcat "\nMCP: Done " cmd-name))
            )
          )

          ;; Clean up command file
          (vl-file-delete cmd-file)
        )
      )
    )
  )
  (princ)
)

;; -----------------------------------------------------------------------
;; Utility helpers (defined if not already loaded from external files)
;; -----------------------------------------------------------------------

(if (not ensure_layer_exists)
  (defun ensure_layer_exists (name color linetype)
    "Create layer if it doesn't exist."
    (if (not (tblsearch "LAYER" name))
      (command "_.-LAYER" "_NEW" name "_COLOR" color name "_LTYPE" linetype name "")
    )
  )
)

(if (not set_current_layer)
  (defun set_current_layer (name)
    "Set a layer as current."
    (setvar "CLAYER" name)
  )
)

(if (not set_attribute_value)
  (defun set_attribute_value (ent tag value / sub-ent ent-data)
    "Set an attribute value on a block insert by tag name."
    (setq sub-ent (entnext ent))
    (while sub-ent
      (setq ent-data (entget sub-ent))
      (if (and (= (cdr (assoc 0 ent-data)) "ATTRIB")
               (= (strcase (cdr (assoc 2 ent-data))) (strcase tag)))
        (progn
          (entmod (subst (cons 1 value) (assoc 1 ent-data) ent-data))
          (entupd sub-ent)
          (setq sub-ent nil)  ; stop
        )
        (if (= (cdr (assoc 0 ent-data)) "SEQEND")
          (setq sub-ent nil)
          (setq sub-ent (entnext sub-ent))
        )
      )
    )
  )
)

;; -----------------------------------------------------------------------
;; Startup message
;; -----------------------------------------------------------------------

(princ "\n=== MCP Dispatch v3.1 loaded ===")
(princ "\nIPC directory: ")
(princ *mcp-ipc-dir*)
(princ "\nReady for commands via (c:mcp-dispatch)")
(princ)

;;; -----------------------------------------------------------------------
;;; MCP Dispatch reliability overrides (v3.2)
;;; -----------------------------------------------------------------------

(setq *mcp-dispatch-version* "3.2")

(defun mcp-error (code message)
  (cons nil (strcat "MCPERR:" code ":" message))
)

(defun mcp-error-code (message / pos rest code-end)
  (if (and message (= (substr message 1 7) "MCPERR:"))
    (progn
      (setq rest (substr message 8))
      (setq code-end (vl-string-search ":" rest))
      (if code-end (substr rest 1 code-end) "command_failed")
    )
    "command_failed"
  )
)

(defun mcp-error-message (message / rest code-end)
  (if (and message (= (substr message 1 7) "MCPERR:"))
    (progn
      (setq rest (substr message 8))
      (setq code-end (vl-string-search ":" rest))
      (if code-end (substr rest (+ code-end 2)) message)
    )
    message
  )
)

(defun mcp-write-result-v32
  (filepath request-id session-id ok-flag payload error-code error-msg / fp tmp-path)
  "Write one atomic result containing request and session identifiers."
  (setq tmp-path (strcat filepath ".tmp"))
  (if (findfile tmp-path) (vl-file-delete tmp-path))
  (setq fp (open tmp-path "w"))
  (if fp
    (progn
      (write-line "{" fp)
      (write-line (strcat "  \"request_id\": \"" (mcp-escape-string request-id) "\",") fp)
      (write-line (strcat "  \"session_id\": \"" (mcp-escape-string session-id) "\",") fp)
      (if ok-flag
        (progn
          (write-line "  \"ok\": true," fp)
          (write-line (strcat "  \"payload\": " payload) fp)
        )
        (progn
          (write-line "  \"ok\": false," fp)
          (write-line (strcat "  \"error_code\": \"" (mcp-escape-string error-code) "\",") fp)
          (write-line (strcat "  \"error\": \"" (mcp-escape-string error-msg) "\"") fp)
        )
      )
      (write-line "}" fp)
      (close fp)
      (if (findfile filepath) (vl-file-delete filepath))
      (vl-file-rename tmp-path filepath)
    )
    (princ (strcat "\nMCP: Cannot open result file: " tmp-path))
  )
)

(defun mcp-save-sysvars (bindings / saved pair)
  (setq saved '())
  (foreach pair bindings
    (setq saved (cons (cons (car pair) (getvar (car pair))) saved))
  )
  saved
)

(defun mcp-restore-sysvars (saved / pair)
  (foreach pair saved
    (vl-catch-all-apply 'setvar (list (car pair) (cdr pair)))
  )
)

(defun mcp-call-with-sysvars (bindings fn args / saved pair result)
  "Set temporary system variables, call FN, and always restore exact old values."
  (setq saved (mcp-save-sysvars bindings))
  (setq result
    (vl-catch-all-apply
      '(lambda ()
         (foreach pair bindings (setvar (car pair) (cadr pair)))
         (apply fn args)
       )
      '()
    )
  )
  (mcp-restore-sysvars saved)
  (if (vl-catch-all-error-p result)
    (mcp-error "command_failed" (vl-catch-all-error-message result))
    result
  )
)

(defun mcp-cancel-owned-command (baseline / guard)
  "Cancel only nested command state created after the dispatcher baseline."
  (setq guard 0)
  (while (and (> (getvar "CMDACTIVE") baseline) (< guard 4))
    (vl-catch-all-apply 'command '())
    (setq guard (1+ guard))
  )
  (= (getvar "CMDACTIVE") baseline)
)

(defun mcp-read-length (text pos / colon value)
  (setq colon (vl-string-search ":" text (1- pos)))
  (if colon
    (list (atoi (substr text pos (- (1+ colon) pos))) (+ colon 2))
    nil
  )
)

(defun mcp-parse-attributes (text / pos total tag-info tag-len tag value-info value-len value result)
  "Decode Python's repeated <tag_len>:<tag><value_len>:<value> format."
  (setq result '() pos 1 total (strlen (if text text "")))
  (while (<= pos total)
    (setq tag-info (mcp-read-length text pos))
    (if (not tag-info)
      (setq pos (1+ total))
      (progn
        (setq tag-len (car tag-info) pos (cadr tag-info))
        (setq tag (substr text pos tag-len) pos (+ pos tag-len))
        (setq value-info (mcp-read-length text pos))
        (if (not value-info)
          (setq pos (1+ total))
          (progn
            (setq value-len (car value-info) pos (cadr value-info))
            (setq value (substr text pos value-len) pos (+ pos value-len))
            (setq result (append result (list (cons tag value))))
          )
        )
      )
    )
  )
  result
)

(defun set_attribute_value (ent tag value / sub-ent ent-data found)
  "Set an attribute and return T only when the requested tag exists."
  (setq found nil sub-ent (entnext ent))
  (while sub-ent
    (setq ent-data (entget sub-ent))
    (cond
      ((= (cdr (assoc 0 ent-data)) "SEQEND") (setq sub-ent nil))
      ((and (= (cdr (assoc 0 ent-data)) "ATTRIB")
            (= (strcase (cdr (assoc 2 ent-data))) (strcase tag)))
       (entmod (subst (cons 1 value) (assoc 1 ent-data) ent-data))
       (entupd sub-ent)
       (setq found T sub-ent nil))
      (t (setq sub-ent (entnext sub-ent)))
    )
  )
  (if found (entupd ent))
  found
)

(defun mcp-apply-attributes (ent attributes / pair missing)
  (setq missing '())
  (foreach pair attributes
    (if (not (set_attribute_value ent (car pair) (cdr pair)))
      (setq missing (append missing (list (car pair))))
    )
  )
  missing
)

(defun mcp-insert-block-raw (name point scale rotation / before ent)
  (setq before (entlast))
  (command "_.-INSERT" name point scale scale rotation)
  (setq ent (entlast))
  (if (or (not ent) (= ent before) (/= (cdr (assoc 0 (entget ent))) "INSERT"))
    (mcp-error "block_insert_failed" "AutoCAD did not create a block reference")
    (cons T ent)
  )
)

(defun mcp-insert-block-safe (name point scale rotation attributes / inserted ent missing)
  (if (not (tblsearch "BLOCK" name))
    (mcp-error "block_not_found" (strcat "Block '" name "' not found"))
    (progn
      (setq inserted
        (mcp-call-with-sysvars
          '(("ATTREQ" 0) ("ATTDIA" 0) ("CMDECHO" 0))
          'mcp-insert-block-raw
          (list name point scale rotation)
        )
      )
      (if (not (car inserted))
        inserted
        (progn
          (setq ent (cdr inserted))
          (setq missing (mcp-apply-attributes ent attributes))
          (if missing
            (progn
              (entdel ent)
              (mcp-error
                "attribute_tag_not_found"
                (strcat "Attribute tag not found: " (car missing))
              )
            )
            (cons T ent)
          )
        )
      )
    )
  )
)

(defun mcp-cmd-block-insert (params / name x y scale rotation block-id attrs inserted ent)
  (setq name (mcp-json-get-string params "name")
        x (mcp-json-get-number params "x")
        y (mcp-json-get-number params "y")
        scale (mcp-json-get-number params "scale")
        rotation (mcp-json-get-number params "rotation")
        block-id (mcp-json-get-string params "block_id"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (setq attrs (if (and block-id (> (strlen block-id) 0)) (list (cons "ID" block-id)) '()))
  (setq inserted (mcp-insert-block-safe name (list x y 0.0) scale rotation attrs))
  (if (car inserted)
    (progn
      (setq ent (cdr inserted))
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\""
                      (cdr (assoc 5 (entget ent))) "\"}")))
    inserted
  )
)

(defun mcp-cmd-block-insert-with-attribs (params / name x y scale rotation attrs-str attrs inserted ent)
  (setq name (mcp-json-get-string params "name")
        x (mcp-json-get-number params "x")
        y (mcp-json-get-number params "y")
        scale (mcp-json-get-number params "scale")
        rotation (mcp-json-get-number params "rotation")
        attrs-str (mcp-json-get-string params "attributes_str"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (setq attrs (mcp-parse-attributes attrs-str))
  (setq inserted (mcp-insert-block-safe name (list x y 0.0) scale rotation attrs))
  (if (car inserted)
    (progn
      (setq ent (cdr inserted))
      (cons T (strcat "{\"entity_type\":\"INSERT\",\"handle\":\""
                      (cdr (assoc 5 (entget ent))) "\"}")))
    inserted
  )
)

(defun mcp-call-pid-block-helper (fn args attributes / before result ent missing)
  "Run an external P&ID insertion helper without dialogs or attribute prompts."
  (setq before (entlast))
  (setq result
    (mcp-call-with-sysvars
      '(("ATTREQ" 0) ("ATTDIA" 0) ("CMDECHO" 0))
      fn
      args
    )
  )
  (if (and (consp result) (not (car result)) (stringp (cdr result))
           (= (substr (cdr result) 1 7) "MCPERR:"))
    result
    (progn
      (setq ent (entlast))
      (if (or (not ent) (= ent before))
        (mcp-error "block_insert_failed" "P&ID helper did not create an entity")
        (progn
          (setq missing (mcp-apply-attributes ent attributes))
          (if missing
            (progn
              (entdel ent)
              (mcp-error "attribute_tag_not_found"
                (strcat "Attribute tag not found: " (car missing))))
            (cons T ent)
          )
        )
      )
    )
  )
)

(defun mcp-cmd-pid-insert-symbol (params / category symbol x y scale rotation inserted ent)
  (setq category (mcp-json-get-string params "category")
        symbol (mcp-json-get-string params "symbol")
        x (mcp-json-get-number params "x")
        y (mcp-json-get-number params "y")
        scale (mcp-json-get-number params "scale")
        rotation (mcp-json-get-number params "rotation"))
  (if (not scale) (setq scale 1.0))
  (if (not rotation) (setq rotation 0.0))
  (if (not c:insert-pid-block)
    (mcp-error "pid_tools_missing" "pid_tools.lsp not loaded")
    (progn
      (setq inserted
        (mcp-call-pid-block-helper 'c:insert-pid-block
          (list category symbol x y scale rotation) '()))
      (if (car inserted)
        (progn
          (setq ent (cdr inserted))
          (cons T (strcat "{\"symbol\":\"" (mcp-escape-string symbol)
                          "\",\"handle\":\"" (cdr (assoc 5 (entget ent))) "\"}")))
        inserted
      )
    )
  )
)

(defun mcp-cmd-pid-insert-valve (params / x y valve-type rotation attrs inserted ent)
  (setq x (mcp-json-get-number params "x")
        y (mcp-json-get-number params "y")
        valve-type (mcp-json-get-string params "valve_type")
        rotation (mcp-json-get-number params "rotation")
        attrs (mcp-parse-attributes (mcp-json-get-string params "attributes_str")))
  (if (not rotation) (setq rotation 0.0))
  (if (not c:insert-valve-on-line)
    (mcp-error "pid_tools_missing" "pid_tools.lsp not loaded")
    (progn
      (setq inserted
        (mcp-call-pid-block-helper 'c:insert-valve-on-line
          (list x y valve-type rotation) attrs))
      (if (car inserted)
        (progn
          (setq ent (cdr inserted))
          (cons T (strcat "{\"valve\":\"" (mcp-escape-string valve-type)
                          "\",\"handle\":\"" (cdr (assoc 5 (entget ent))) "\"}")))
        inserted
      )
    )
  )
)

(defun mcp-cmd-pid-insert-pump (params / x y pump-type rotation attrs inserted ent)
  (setq x (mcp-json-get-number params "x")
        y (mcp-json-get-number params "y")
        pump-type (mcp-json-get-string params "pump_type")
        rotation (mcp-json-get-number params "rotation")
        attrs (mcp-parse-attributes (mcp-json-get-string params "attributes_str")))
  (if (not rotation) (setq rotation 0.0))
  (if (not c:insert-pump)
    (mcp-error "pid_tools_missing" "pid_tools.lsp not loaded")
    (progn
      (setq inserted
        (mcp-call-pid-block-helper 'c:insert-pump
          (list x y pump-type rotation) attrs))
      (if (car inserted)
        (progn
          (setq ent (cdr inserted))
          (cons T (strcat "{\"pump\":\"" (mcp-escape-string pump-type)
                          "\",\"handle\":\"" (cdr (assoc 5 (entget ent))) "\"}")))
        inserted
      )
    )
  )
)

(defun mcp-cmd-pid-insert-tank (params / x y tank-type scale attrs inserted ent)
  (setq x (mcp-json-get-number params "x")
        y (mcp-json-get-number params "y")
        tank-type (mcp-json-get-string params "tank_type")
        scale (mcp-json-get-number params "scale")
        attrs (mcp-parse-attributes (mcp-json-get-string params "attributes_str")))
  (if (not scale) (setq scale 1.0))
  (if (not c:insert-tank)
    (mcp-error "pid_tools_missing" "pid_tools.lsp not loaded")
    (progn
      (setq inserted
        (mcp-call-pid-block-helper 'c:insert-tank
          (list x y tank-type scale) attrs))
      (if (car inserted)
        (progn
          (setq ent (cdr inserted))
          (cons T (strcat "{\"tank\":\"" (mcp-escape-string tank-type)
                          "\",\"handle\":\"" (cdr (assoc 5 (entget ent))) "\"}")))
        inserted
      )
    )
  )
)

(defun mcp-load-code-file-raw (code-file / result)
  (setq result (vl-catch-all-apply 'load (list code-file)))
  (if (vl-catch-all-error-p result)
    (mcp-error "lisp_error" (vl-catch-all-error-message result))
    (cons T (strcat "\"" (mcp-escape-string (vl-princ-to-string result)) "\""))
  )
)

(defun mcp-cmd-execute-lisp (params / code-file)
  (setq code-file (mcp-json-get-string params "code_file"))
  (if (not (findfile code-file))
    (mcp-error "code_file_not_found" "Code file not found")
    (mcp-call-with-sysvars '(("SECURELOAD" 0)) 'mcp-load-code-file-raw (list code-file))
  )
)

(defun mcp-saveas-raw (path)
  (command "_.SAVEAS" "" path)
  (cons T (strcat "\"saved to: " (mcp-escape-string path) "\""))
)

(defun mcp-open-raw (path)
  (command "_.OPEN" path)
  (cons T (strcat "\"opened: " (mcp-escape-string path) "\""))
)

(defun mcp-save-dxf-raw (path)
  (command "_.SAVEAS" "DXF" path)
  (cons T (strcat "\"" (mcp-escape-string path) "\""))
)

(defun mcp-dispatch-command (cmd-name params-json / path)
  "Reliability wrapper around the original v3.1 command map."
  (cond
    ((= cmd-name "drawing-save")
     (setq path (mcp-json-get-string params-json "path"))
     (if (and path (> (strlen path) 0))
       (mcp-call-with-sysvars '(("FILEDIA" 0) ("CMDECHO" 0)) 'mcp-saveas-raw (list path))
       (mcp-dispatch-command-v31 cmd-name params-json)))
    ((= cmd-name "drawing-save-as-dxf")
     (setq path (mcp-json-get-string params-json "path"))
     (if path
       (mcp-call-with-sysvars '(("FILEDIA" 0) ("CMDECHO" 0)) 'mcp-save-dxf-raw (list path))
       (mcp-error "path_required" "Save path required")))
    ((= cmd-name "drawing-open")
     (setq path (mcp-json-get-string params-json "path"))
     (if path
       (mcp-call-with-sysvars '(("FILEDIA" 0) ("CMDECHO" 0)) 'mcp-open-raw (list path))
       (mcp-error "path_required" "Open path required")))
    (t (mcp-dispatch-command-v31 cmd-name params-json))
  )
)

(defun mcp-process-request
  (session-id request-id / cmd-file result-file json-text cmd-name payload-request payload-session result before error-text)
  (setq cmd-file
    (strcat *mcp-ipc-dir* "autocad_mcp_cmd_" session-id "_" request-id ".json"))
  (setq result-file
    (strcat *mcp-ipc-dir* "autocad_mcp_result_" session-id "_" request-id ".json"))
  (if (not (findfile cmd-file))
    (mcp-write-result-v32 result-file request-id session-id nil nil
      "ipc_command_missing" "The exact IPC command file was not found")
    (progn
      (setq json-text (mcp-read-file-lines cmd-file))
      (setq cmd-name (if json-text (mcp-json-get-string json-text "command") nil))
      (setq payload-request (if json-text (mcp-json-get-string json-text "request_id") nil))
      (setq payload-session (if json-text (mcp-json-get-string json-text "session_id") nil))
      (cond
        ((not json-text)
         (mcp-write-result-v32 result-file request-id session-id nil nil
           "ipc_command_invalid" "Cannot read command file"))
        ((not cmd-name)
         (mcp-write-result-v32 result-file request-id session-id nil nil
           "ipc_command_invalid" "Command name is missing"))
        ((or (/= payload-request request-id) (/= payload-session session-id))
         (mcp-write-result-v32 result-file request-id session-id nil nil
           "ipc_command_invalid" "Command file identifiers do not match its filename"))
        (t
         ;; PostCommand starts this dispatcher only when the document is ready.
         ;; Record the dispatcher command's own CMDACTIVE baseline so cleanup
         ;; never cancels a command that existed before MCP routing.
         (setq before (getvar "CMDACTIVE"))
         (setq result
           (vl-catch-all-apply 'mcp-dispatch-command (list cmd-name json-text)))
         (if (vl-catch-all-error-p result)
           (setq result (mcp-error "command_failed" (vl-catch-all-error-message result))))
         (if (> (getvar "CMDACTIVE") before)
           (progn
             (mcp-cancel-owned-command before)
             (setq result
               (mcp-error "command_not_completed"
                 "MCP command left AutoCAD waiting for input and was cancelled"))))
         (if (car result)
           (mcp-write-result-v32 result-file request-id session-id T (cdr result) nil nil)
           (progn
             (setq error-text (cdr result))
             (mcp-write-result-v32 result-file request-id session-id nil nil
               (mcp-error-code error-text) (mcp-error-message error-text))))
        )
      )
      (vl-file-delete cmd-file)
    )
  )
)

(defun c:mcp-dispatch-request (session-id request-id)
  "Process one exact request; never scan or select another process's file."
  (if (and session-id request-id)
    (mcp-process-request session-id request-id)
    (princ "\nMCP: session-id and request-id are required"))
  (princ)
)

(defun c:mcp-dispatch (/ cmd-files filename parts session-id request-id stem first-sep)
  "Legacy manual entry point. Sort files and process only the oldest name deterministically."
  (setq cmd-files
    (acad_strlsort (vl-directory-files *mcp-ipc-dir* "autocad_mcp_cmd_*.json" 1)))
  (if (not cmd-files)
    (princ "\nMCP: No pending commands")
    (progn
      (setq filename (car cmd-files))
      (setq stem (substr filename 17 (- (strlen filename) 21)))
      (setq first-sep (vl-string-search "_" stem))
      (if first-sep
        (progn
          (setq session-id (substr stem 1 first-sep))
          (setq request-id (substr stem (+ first-sep 2)))
          (mcp-process-request session-id request-id))
        (princ "\nMCP: Legacy command filename is invalid"))
    )
  )
  (princ)
)

(princ "\n=== MCP Dispatch v3.2 reliability overrides loaded ===")
(princ)
