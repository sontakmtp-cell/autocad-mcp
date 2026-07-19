;;; auto_dimension.lsp — Local one-call 2D automatic dimensioning engine.
;;; Compatible with AutoCAD LT 2024+ and does not require ActiveX/COM.
;;;
;;; Public entry point:
;;;   (mcp-auto-dimension mode include-overall include-features include-holes
;;;     include-arcs include-centers detect-symmetry clear-existing zoom-preview
;;;     dimension-layer spacing source-layers report-file)

(defun mcp-ad-json-escape (value / text result index char)
  (setq text (if value value ""))
  (setq result "" index 1)
  (while (<= index (strlen text))
    (setq char (substr text index 1))
    (cond
      ((= char "\\") (setq result (strcat result "\\\\")))
      ((= char "\"") (setq result (strcat result "\\\"")))
      (t (setq result (strcat result char)))
    )
    (setq index (1+ index))
  )
  result
)

(defun mcp-ad-json-bool (value)
  (if value "true" "false")
)

(defun mcp-ad-write-error (report-file message / fp)
  (if (and report-file (> (strlen report-file) 0))
    (progn
      (setq fp (open report-file "w"))
      (if fp
        (progn
          (write-line
            (strcat
              "{\"ok\":false,\"error\":\""
              (mcp-ad-json-escape message)
              "\"}"
            )
            fp
          )
          (close fp)
        )
      )
    )
  )
)

(defun mcp-ad-write-success
  (report-file mode geometry-count unsupported-count circle-count arc-entity-count
   dimension-count overall-count feature-count hole-count radius-count center-count
   symmetry-count vertical-pairs horizontal-pairs skipped-short dim-layer spacing
   min-x min-y max-x max-y / fp payload)
  (if (and report-file (> (strlen report-file) 0))
    (progn
      (setq payload
        (strcat
          "{"
          "\"ok\":true,"
          "\"backend\":\"file_ipc\","
          "\"mode\":\"" (mcp-ad-json-escape mode) "\","
          "\"geometry_count\":" (itoa geometry-count) ","
          "\"unsupported_entities\":" (itoa unsupported-count) ","
          "\"circle_count\":" (itoa circle-count) ","
          "\"arc_count\":" (itoa arc-entity-count) ","
          "\"dimensions_created\":" (itoa dimension-count) ","
          "\"overall_dimensions\":" (itoa overall-count) ","
          "\"feature_dimensions\":" (itoa feature-count) ","
          "\"hole_dimensions\":" (itoa hole-count) ","
          "\"arc_dimensions\":" (itoa radius-count) ","
          "\"center_marks\":" (itoa center-count) ","
          "\"symmetry_dimensions\":" (itoa symmetry-count) ","
          "\"vertical_symmetry_pairs\":" (itoa vertical-pairs) ","
          "\"horizontal_symmetry_pairs\":" (itoa horizontal-pairs) ","
          "\"skipped_short_segments\":" (itoa skipped-short) ","
          "\"dimension_layer\":\"" (mcp-ad-json-escape dim-layer) "\","
          "\"spacing\":" (rtos spacing 2 6) ","
          "\"extents\":{"
            "\"min\":[" (rtos min-x 2 6) "," (rtos min-y 2 6) "],"
            "\"max\":[" (rtos max-x 2 6) "," (rtos max-y 2 6) "]"
          "},"
          "\"preview\":\"attached when include_screenshot=true\""
          "}"
        )
      )
      (setq fp (open report-file "w"))
      (if fp
        (progn (write-line payload fp) (close fp))
      )
    )
  )
)

(defun mcp-ad-layer-allowed-p (layer dim-layer source-layers)
  (and
    (/= (strcase layer) (strcase dim-layer))
    (/= (strcase layer) "DEFPOINTS")
    (or
      (not source-layers)
      (member (strcase layer) (mapcar 'strcase source-layers))
    )
  )
)

(defun mcp-ad-update-extents (extents x y / min-x min-y max-x max-y)
  (if (not extents)
    (list x y x y)
    (progn
      (setq min-x (min (nth 0 extents) x))
      (setq min-y (min (nth 1 extents) y))
      (setq max-x (max (nth 2 extents) x))
      (setq max-y (max (nth 3 extents) y))
      (list min-x min-y max-x max-y)
    )
  )
)

(defun mcp-ad-add-point (state x y / extents points x-values y-values)
  ;; state = (extents points x-values y-values)
  (setq extents (mcp-ad-update-extents (nth 0 state) x y))
  (setq points (cons (list x y) (nth 1 state)))
  (setq x-values (cons x (nth 2 state)))
  (setq y-values (cons y (nth 3 state)))
  (list extents points x-values y-values)
)

(defun mcp-ad-sort-numbers (values)
  (vl-sort values '<)
)

(defun mcp-ad-unique-sorted (values tolerance / ordered result value)
  (setq ordered (mcp-ad-sort-numbers values))
  (setq result '())
  (foreach value ordered
    (if (or (not result) (> (abs (- value (car result))) tolerance))
      (setq result (cons value result))
    )
  )
  (reverse result)
)

(defun mcp-ad-nth-safe (index values)
  (if (and (>= index 0) (< index (length values))) (nth index values) nil)
)

(defun mcp-ad-thin-coordinates (values cap / count result index source-index last-value)
  (setq count (length values))
  (if (<= count cap)
    values
    (progn
      (setq result (list (car values)))
      (setq index 1)
      (while (< index (1- cap))
        (setq source-index (fix (+ 0.5 (* index (/ (float (1- count)) (float (1- cap)))))))
        (setq result (append result (list (mcp-ad-nth-safe source-index values))))
        (setq index (1+ index))
      )
      (setq last-value (nth (1- count) values))
      (append result (list last-value))
    )
  )
)

(defun mcp-ad-run-command (arguments / result)
  (setq result (vl-catch-all-apply 'command arguments))
  (not (vl-catch-all-error-p result))
)

(defun mcp-ad-ensure-layer (layer / old-cmdecho)
  (if (not (tblsearch "LAYER" layer))
    (progn
      (setq old-cmdecho (getvar "CMDECHO"))
      (setvar "CMDECHO" 0)
      (command "_.-LAYER" "_NEW" layer "_COLOR" "2" layer "")
      (setvar "CMDECHO" old-cmdecho)
    )
  )
)

(defun mcp-ad-count-model-dimensions (/ selection)
  (setq selection (ssget "_X" '((0 . "DIMENSION") (410 . "Model"))))
  (if selection (sslength selection) 0)
)

(defun mcp-ad-clear-generated-layer (layer / selection)
  ;; The layer is dedicated to this tool, so remove dimensions and center marks.
  (setq selection
    (ssget "_X" (list '(410 . "Model") (cons 8 layer)))
  )
  (if selection
    (mcp-ad-run-command (list "_.ERASE" selection ""))
  )
)

(defun mcp-ad-collect-geometry
  (dim-layer source-layers / selection index entity data entity-type layer state
   extents points x-values y-values circles arcs geometry-count unsupported-count
   point start end center radius item vertex-data vertex type-code major ratio
   major-length ux uy vx vy minor-length x-radius y-radius)
  (setq state (list nil '() '() '()))
  (setq circles '() arcs '() geometry-count 0 unsupported-count 0)
  (setq selection
    (ssget "_X" '((0 . "LINE,LWPOLYLINE,POLYLINE,CIRCLE,ARC,ELLIPSE") (410 . "Model")))
  )
  (if selection
    (progn
      (setq index 0)
      (while (< index (sslength selection))
        (setq entity (ssname selection index))
        (setq data (entget entity))
        (setq entity-type (cdr (assoc 0 data)))
        (setq layer (cdr (assoc 8 data)))
        (if (mcp-ad-layer-allowed-p layer dim-layer source-layers)
          (cond
            ((= entity-type "LINE")
              (setq start (cdr (assoc 10 data)))
              (setq end (cdr (assoc 11 data)))
              (setq state (mcp-ad-add-point state (car start) (cadr start)))
              (setq state (mcp-ad-add-point state (car end) (cadr end)))
              (setq geometry-count (1+ geometry-count))
            )
            ((= entity-type "LWPOLYLINE")
              (foreach item data
                (if (= (car item) 10)
                  (progn
                    (setq point (cdr item))
                    (setq state (mcp-ad-add-point state (car point) (cadr point)))
                  )
                )
              )
              (setq geometry-count (1+ geometry-count))
            )
            ((= entity-type "POLYLINE")
              (setq vertex (entnext entity))
              (while vertex
                (setq vertex-data (entget vertex))
                (setq type-code (cdr (assoc 0 vertex-data)))
                (cond
                  ((= type-code "VERTEX")
                    (setq point (cdr (assoc 10 vertex-data)))
                    (setq state (mcp-ad-add-point state (car point) (cadr point)))
                  )
                  ((= type-code "SEQEND") (setq vertex nil))
                )
                (if vertex (setq vertex (entnext vertex)))
              )
              (setq geometry-count (1+ geometry-count))
            )
            ((= entity-type "CIRCLE")
              (setq center (cdr (assoc 10 data)))
              (setq radius (cdr (assoc 40 data)))
              (setq state (mcp-ad-add-point state (- (car center) radius) (- (cadr center) radius)))
              (setq state (mcp-ad-add-point state (+ (car center) radius) (+ (cadr center) radius)))
              (setq state (mcp-ad-add-point state (car center) (cadr center)))
              (setq circles (cons (list entity (car center) (cadr center) radius) circles))
              (setq geometry-count (1+ geometry-count))
            )
            ((= entity-type "ARC")
              (setq center (cdr (assoc 10 data)))
              (setq radius (cdr (assoc 40 data)))
              (setq state (mcp-ad-add-point state (- (car center) radius) (- (cadr center) radius)))
              (setq state (mcp-ad-add-point state (+ (car center) radius) (+ (cadr center) radius)))
              (setq state (mcp-ad-add-point state (car center) (cadr center)))
              (setq arcs (cons (list entity (car center) (cadr center) radius) arcs))
              (setq geometry-count (1+ geometry-count))
            )
            ((= entity-type "ELLIPSE")
              (setq center (cdr (assoc 10 data)))
              (setq major (cdr (assoc 11 data)))
              (setq ratio (cdr (assoc 40 data)))
              (setq major-length (distance '(0.0 0.0 0.0) major))
              (if (> major-length 0.0)
                (progn
                  (setq ux (/ (car major) major-length))
                  (setq uy (/ (cadr major) major-length))
                  (setq vx (- uy))
                  (setq vy ux)
                  (setq minor-length (* major-length ratio))
                  (setq x-radius
                    (sqrt (+ (* major-length major-length ux ux)
                             (* minor-length minor-length vx vx))))
                  (setq y-radius
                    (sqrt (+ (* major-length major-length uy uy)
                             (* minor-length minor-length vy vy))))
                  (setq state (mcp-ad-add-point state (- (car center) x-radius) (- (cadr center) y-radius)))
                  (setq state (mcp-ad-add-point state (+ (car center) x-radius) (+ (cadr center) y-radius)))
                  (setq state (mcp-ad-add-point state (car center) (cadr center)))
                )
              )
              (setq geometry-count (1+ geometry-count))
            )
            (t (setq unsupported-count (1+ unsupported-count)))
          )
        )
        (setq index (1+ index))
      )
    )
  )
  (setq extents (nth 0 state))
  (setq points (nth 1 state))
  (setq x-values (nth 2 state))
  (setq y-values (nth 3 state))
  (list extents points x-values y-values (reverse circles) (reverse arcs)
        geometry-count unsupported-count)
)

(defun mcp-ad-run
  (mode include-overall include-features include-holes include-arcs include-centers
   detect-symmetry clear-existing zoom-preview dim-layer requested-spacing source-layers
   report-file / collected extents points x-values y-values circles arcs geometry-count
   unsupported-count min-x min-y max-x max-y width height scale dimscale dimtxt spacing
   tolerance min-segment mode-cap existing-dims first-lane chain-lane dimensions-created
   overall-count feature-count hole-count radius-count center-count symmetry-count
   vertical-pairs horizontal-pairs skipped-short x-coordinates y-coordinates pair left right
   bottom top circle arc index angle lane distance leader-point angles mark center-x center-y
   symmetry-tolerance c1 c2 pair-index base-x base-y old-layer)

  (mcp-ad-ensure-layer dim-layer)
  (if clear-existing (mcp-ad-clear-generated-layer dim-layer))
  (setq existing-dims (mcp-ad-count-model-dimensions))
  (setq collected (mcp-ad-collect-geometry dim-layer source-layers))
  (setq extents (nth 0 collected))
  (setq points (nth 1 collected))
  (setq x-values (nth 2 collected))
  (setq y-values (nth 3 collected))
  (setq circles (nth 4 collected))
  (setq arcs (nth 5 collected))
  (setq geometry-count (nth 6 collected))
  (setq unsupported-count (nth 7 collected))

  (if (or (not extents) (= geometry-count 0))
    (progn
      (mcp-ad-write-error report-file
        "No supported Model Space geometry found (LINE, POLYLINE, CIRCLE, ARC, ELLIPSE).")
      nil
    )
    (progn
      (setq min-x (nth 0 extents))
      (setq min-y (nth 1 extents))
      (setq max-x (nth 2 extents))
      (setq max-y (nth 3 extents))
      (setq width (- max-x min-x))
      (setq height (- max-y min-y))
      (setq scale (max width height 1.0))
      (setq dimscale (getvar "DIMSCALE"))
      (if (or (not dimscale) (<= dimscale 0.0)) (setq dimscale 1.0))
      (setq dimtxt (getvar "DIMTXT"))
      (if (or (not dimtxt) (<= dimtxt 0.0)) (setq dimtxt 2.5))
      (if (> requested-spacing 0.0)
        (setq spacing requested-spacing)
        (setq spacing (max (* scale 0.045) (* dimtxt dimscale 4.0) 5.0))
      )
      (setq tolerance (max (* scale 0.00001) 0.000001))
      (setq min-segment (* spacing 0.65))
      (cond
        ((= mode "minimal") (setq mode-cap 2))
        ((= mode "detailed") (setq mode-cap 24))
        (t (setq mode-cap 12))
      )
      (setq first-lane (* spacing (+ 1.5 (* (min existing-dims 4) 0.35))))
      (setq chain-lane (+ first-lane spacing))
      (setq dimensions-created 0 overall-count 0 feature-count 0 hole-count 0)
      (setq radius-count 0 center-count 0 symmetry-count 0)
      (setq vertical-pairs 0 horizontal-pairs 0 skipped-short 0)
      (setq old-layer (getvar "CLAYER"))
      (setvar "CLAYER" dim-layer)

      ;; Overall extents always occupy the first outside lane.
      (if include-overall
        (progn
          (if (mcp-ad-run-command
                (list "_.DIMLINEAR"
                      (list min-x min-y 0.0)
                      (list max-x min-y 0.0)
                      (list min-x (- min-y first-lane) 0.0)))
            (setq dimensions-created (1+ dimensions-created)
                  overall-count (1+ overall-count))
          )
          (if (mcp-ad-run-command
                (list "_.DIMLINEAR"
                      (list min-x min-y 0.0)
                      (list min-x max-y 0.0)
                      (list (- min-x first-lane) min-y 0.0)))
            (setq dimensions-created (1+ dimensions-created)
                  overall-count (1+ overall-count))
          )
        )
      )

      ;; Feature chains use a second lane and skip tiny intervals.
      (if (and include-features (/= mode "minimal"))
        (progn
          (setq x-coordinates
            (mcp-ad-thin-coordinates
              (mcp-ad-unique-sorted x-values tolerance) mode-cap))
          (setq y-coordinates
            (mcp-ad-thin-coordinates
              (mcp-ad-unique-sorted y-values tolerance) mode-cap))

          (if (> (length x-coordinates) 2)
            (progn
              (setq index 0)
              (while (< index (1- (length x-coordinates)))
                (setq left (nth index x-coordinates))
                (setq right (nth (1+ index) x-coordinates))
                (if (< (- right left) min-segment)
                  (setq skipped-short (1+ skipped-short))
                  (if (mcp-ad-run-command
                        (list "_.DIMLINEAR"
                              (list left min-y 0.0)
                              (list right min-y 0.0)
                              (list left (- min-y chain-lane) 0.0)))
                    (setq dimensions-created (1+ dimensions-created)
                          feature-count (1+ feature-count))
                  )
                )
                (setq index (1+ index))
              )
            )
          )

          (if (> (length y-coordinates) 2)
            (progn
              (setq index 0)
              (while (< index (1- (length y-coordinates)))
                (setq bottom (nth index y-coordinates))
                (setq top (nth (1+ index) y-coordinates))
                (if (< (- top bottom) min-segment)
                  (setq skipped-short (1+ skipped-short))
                  (if (mcp-ad-run-command
                        (list "_.DIMLINEAR"
                              (list min-x bottom 0.0)
                              (list min-x top 0.0)
                              (list (- min-x chain-lane) bottom 0.0)))
                    (setq dimensions-created (1+ dimensions-created)
                          feature-count (1+ feature-count))
                  )
                )
                (setq index (1+ index))
              )
            )
          )
        )
      )

      ;; Diameter/radius leaders rotate through four quadrants and extra lanes.
      (setq angles (list (/ pi 4.0) (* 3.0 (/ pi 4.0))
                         (* 5.0 (/ pi 4.0)) (* 7.0 (/ pi 4.0))))
      (if include-holes
        (progn
          (setq index 0)
          (foreach circle circles
            (setq angle (nth (rem index 4) angles))
            (setq lane (1+ (fix (/ index 4))))
            (setq distance (+ (nth 3 circle) (* spacing lane)))
            (setq leader-point
              (list
                (+ (nth 1 circle) (* distance (cos angle)))
                (+ (nth 2 circle) (* distance (sin angle)))
                0.0))
            (if (mcp-ad-run-command
                  (list "_.DIMDIAMETER" (nth 0 circle) leader-point))
              (setq dimensions-created (1+ dimensions-created)
                    hole-count (1+ hole-count))
            )
            (if include-centers
              (if (mcp-ad-run-command (list "_.DIMCENTER" (nth 0 circle)))
                (setq center-count (1+ center-count))
              )
            )
            (setq index (1+ index))
          )
        )
      )

      (if include-arcs
        (progn
          (setq index 0)
          (foreach arc arcs
            (setq angle (nth (rem (1+ index) 4) angles))
            (setq lane (1+ (fix (/ index 4))))
            (setq distance (+ (nth 3 arc) (* spacing lane)))
            (setq leader-point
              (list
                (+ (nth 1 arc) (* distance (cos angle)))
                (+ (nth 2 arc) (* distance (sin angle)))
                0.0))
            (if (mcp-ad-run-command
                  (list "_.DIMRADIUS" (nth 0 arc) leader-point))
              (setq dimensions-created (1+ dimensions-created)
                    radius-count (1+ radius-count))
            )
            (setq index (1+ index))
          )
        )
      )

      ;; Detect equal-radius hole pairs mirrored about the overall center axes.
      (if (and detect-symmetry (>= (length circles) 2))
        (progn
          (setq center-x (/ (+ min-x max-x) 2.0))
          (setq center-y (/ (+ min-y max-y) 2.0))
          (setq symmetry-tolerance (max (* tolerance 10.0) (* spacing 0.08)))
          (setq pair-index 0)
          (foreach c1 circles
            (foreach c2 circles
              (if (and
                    (< (nth 1 c1) (- center-x symmetry-tolerance))
                    (> (nth 1 c2) (+ center-x symmetry-tolerance))
                    (<= (abs (- (+ (nth 1 c1) (nth 1 c2)) (* 2.0 center-x))) symmetry-tolerance)
                    (<= (abs (- (nth 2 c1) (nth 2 c2))) symmetry-tolerance)
                    (<= (abs (- (nth 3 c1) (nth 3 c2))) symmetry-tolerance)
                    (< vertical-pairs 8))
                (progn
                  (setq base-y (+ max-y first-lane (* spacing (1+ pair-index))))
                  (if (mcp-ad-run-command
                        (list "_.DIMLINEAR"
                              (list (nth 1 c1) (nth 2 c1) 0.0)
                              (list (nth 1 c2) (nth 2 c2) 0.0)
                              (list (nth 1 c1) base-y 0.0)))
                    (setq dimensions-created (1+ dimensions-created)
                          symmetry-count (1+ symmetry-count))
                  )
                  (setq vertical-pairs (1+ vertical-pairs))
                  (setq pair-index (1+ pair-index))
                )
              )
            )
          )

          (setq pair-index 0)
          (foreach c1 circles
            (foreach c2 circles
              (if (and
                    (< (nth 2 c1) (- center-y symmetry-tolerance))
                    (> (nth 2 c2) (+ center-y symmetry-tolerance))
                    (<= (abs (- (+ (nth 2 c1) (nth 2 c2)) (* 2.0 center-y))) symmetry-tolerance)
                    (<= (abs (- (nth 1 c1) (nth 1 c2))) symmetry-tolerance)
                    (<= (abs (- (nth 3 c1) (nth 3 c2))) symmetry-tolerance)
                    (< horizontal-pairs 8))
                (progn
                  (setq base-x (+ max-x first-lane (* spacing (1+ pair-index))))
                  (if (mcp-ad-run-command
                        (list "_.DIMLINEAR"
                              (list (nth 1 c1) (nth 2 c1) 0.0)
                              (list (nth 1 c2) (nth 2 c2) 0.0)
                              (list base-x (nth 2 c1) 0.0)))
                    (setq dimensions-created (1+ dimensions-created)
                          symmetry-count (1+ symmetry-count))
                  )
                  (setq horizontal-pairs (1+ horizontal-pairs))
                  (setq pair-index (1+ pair-index))
                )
              )
            )
          )
        )
      )

      (setvar "CLAYER" old-layer)
      (if zoom-preview (mcp-ad-run-command (list "_.ZOOM" "_E")))
      (mcp-ad-write-success
        report-file mode geometry-count unsupported-count (length circles) (length arcs)
        dimensions-created overall-count feature-count hole-count radius-count center-count
        symmetry-count vertical-pairs horizontal-pairs skipped-short dim-layer spacing
        min-x min-y max-x max-y)
      T
    )
  )
)

(defun mcp-auto-dimension
  (mode include-overall include-features include-holes include-arcs include-centers
   detect-symmetry clear-existing zoom-preview dim-layer requested-spacing source-layers
   report-file / old-layer old-cmdecho result)
  "Analyze Model Space and create a deterministic, outside-lane dimension layout."
  (setq old-layer (getvar "CLAYER"))
  (setq old-cmdecho (getvar "CMDECHO"))
  (setvar "CMDECHO" 0)
  (command "_.UNDO" "_BEGIN")
  (setq result
    (vl-catch-all-apply
      'mcp-ad-run
      (list mode include-overall include-features include-holes include-arcs
            include-centers detect-symmetry clear-existing zoom-preview dim-layer
            requested-spacing source-layers report-file)
    )
  )
  (if (vl-catch-all-error-p result)
    (mcp-ad-write-error report-file (vl-catch-all-error-message result))
  )
  (command "_.UNDO" "_END")
  (if (tblsearch "LAYER" old-layer) (setvar "CLAYER" old-layer))
  (setvar "CMDECHO" old-cmdecho)
  (if (vl-catch-all-error-p result) nil result)
)

(princ "\nMCP automatic dimensioning engine loaded.")
(princ)
