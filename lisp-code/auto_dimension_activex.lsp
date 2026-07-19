;;; Phase 2 ActiveX dimension commit engine.
;;; Loaded after auto_dimension.lsp by auto_dimension_loader.lsp.
;;;
;;; The planning/export workflow is unchanged. Only the final drawing mutation is
;;; replaced: dimensions are created directly through ModelSpace.AddDim* instead
;;; of starting AutoCAD DIM commands once per instruction.

(vl-load-com)

(setq *mcp-ad-activex-engine-version* "phase2-2026-07-19")

(defun mcp-ad-vla-point (point)
  (vlax-3d-point
    (list
      (float (car point))
      (float (cadr point))
      (if (caddr point) (float (caddr point)) 0.0)))
)

(defun mcp-ad-vla-item (collection name / result)
  (setq result (vl-catch-all-apply 'vla-Item (list collection name)))
  (if (vl-catch-all-error-p result) nil result)
)

(defun mcp-ad-vla-put (object property value / result)
  (setq result
    (vl-catch-all-apply
      'vlax-put-property
      (list object property value)))
  (not (vl-catch-all-error-p result))
)

(defun mcp-ad-vla-require-put (object property value message)
  (if (not (mcp-ad-vla-put object property value))
    (error message))
  T
)

(defun mcp-ad-vla-ensure-layer (document name / layers layer)
  (setq layers (vla-get-Layers document))
  (setq layer (mcp-ad-vla-item layers name))
  (if (not layer)
    (setq layer (vla-Add layers name)))
  (if layer
    (vl-catch-all-apply 'vla-put-Color (list layer 2)))
  layer
)

(defun mcp-ad-vla-ensure-dimstyle (document name / styles style)
  (setq styles (vla-get-DimStyles document))
  (setq style (mcp-ad-vla-item styles name))
  (if (not style)
    (progn
      (setq style (vla-Add styles name))
      ;; Seed a newly created style from the current document plus overrides.
      (vl-catch-all-apply 'vla-CopyFrom (list style document))))
  style
)

(defun mcp-ad-vla-clear-generated-layer (layer / selection index entity)
  (setq selection
    (ssget "_X" (list '(410 . "Model") (cons 8 layer))))
  (if selection
    (progn
      (setq index (1- (sslength selection)))
      (while (>= index 0)
        (setq entity (ssname selection index))
        (if entity (entdel entity))
        (setq index (1- index)))))
  T
)

(defun mcp-ad-vla-configure-dimension
  (dimension layer dimstyle scale-factor text-height arrow-size precision
   tolerance-mode tolerance-upper tolerance-lower text / tolerance-display)
  (mcp-ad-vla-require-put dimension 'Layer layer
    "Could not set dimension layer.")
  (if (and dimstyle (> (strlen dimstyle) 0))
    (mcp-ad-vla-require-put dimension 'StyleName dimstyle
      "Could not set dimension style."))
  (if (> scale-factor 0.0)
    (mcp-ad-vla-require-put dimension 'ScaleFactor scale-factor
      "Could not set dimension scale."))
  (if (> text-height 0.0)
    (mcp-ad-vla-require-put dimension 'TextHeight text-height
      "Could not set dimension text height."))
  (if (> arrow-size 0.0)
    (mcp-ad-vla-require-put dimension 'ArrowheadSize arrow-size
      "Could not set dimension arrow size."))
  (if (and (>= precision 0) (<= precision 8))
    (mcp-ad-vla-require-put dimension 'PrimaryUnitsPrecision precision
      "Could not set dimension precision."))

  ;; acTolNone=0, acTolSymmetrical=1, acTolDeviation=2.
  (cond
    ((= tolerance-mode "symmetric") (setq tolerance-display 1))
    ((= tolerance-mode "deviation") (setq tolerance-display 2))
    (t (setq tolerance-display 0)))
  (mcp-ad-vla-require-put dimension 'ToleranceDisplay tolerance-display
    "Could not set dimension tolerance mode.")
  (if (> tolerance-display 0)
    (progn
      (mcp-ad-vla-require-put dimension 'ToleranceUpperLimit tolerance-upper
        "Could not set upper dimension tolerance.")
      (mcp-ad-vla-require-put dimension 'ToleranceLowerLimit tolerance-lower
        "Could not set lower dimension tolerance.")))

  (if (and text (> (strlen text) 0))
    (mcp-ad-vla-require-put dimension 'TextOverride text
      "Could not set dimension text override."))
  dimension
)

(defun mcp-ad-vla-radial-data
  (handle label-point / entity data entity-type center radius dx dy length ux uy
   chord far-chord leader-length)
  (setq entity (handent handle))
  (if (not entity)
    (error (strcat "Dimension source handle was not found: " handle)))
  (setq data (entget entity))
  (setq entity-type (cdr (assoc 0 data)))
  (if (not (member entity-type '("CIRCLE" "ARC")))
    (error (strcat "Dimension source is not a circle or arc: " handle)))
  (setq center (cdr (assoc 10 data)))
  (setq radius (cdr (assoc 40 data)))
  (if (or (not center) (not radius) (<= radius 0.0))
    (error (strcat "Dimension source has invalid radial geometry: " handle)))

  (setq dx (- (car label-point) (car center))
        dy (- (cadr label-point) (cadr center))
        length (sqrt (+ (* dx dx) (* dy dy))))
  (if (<= length 1e-9)
    (setq ux 1.0 uy 0.0)
    (setq ux (/ dx length) uy (/ dy length)))
  (setq chord
    (list (+ (car center) (* radius ux))
          (+ (cadr center) (* radius uy))
          0.0))
  (setq far-chord
    (list (- (car center) (* radius ux))
          (- (cadr center) (* radius uy))
          0.0))
  (setq leader-length
    (max 0.001 (abs (- (distance center label-point) radius))))
  (list center radius chord far-chord leader-length)
)

(defun mcp-ad-vla-create-linear
  (model-space item layer dimstyle scale-factor text-height arrow-size precision
   tolerance-mode tolerance-upper tolerance-lower
   / point1 point2 location angle dimension text)
  (setq point1 (nth 1 item)
        point2 (nth 2 item)
        location (nth 3 item)
        angle (nth 4 item)
        text (nth 5 item))
  (setq dimension
    (vla-AddDimRotated
      model-space
      (mcp-ad-vla-point point1)
      (mcp-ad-vla-point point2)
      (mcp-ad-vla-point location)
      (* pi (/ (float angle) 180.0))))
  (mcp-ad-vla-configure-dimension
    dimension layer dimstyle scale-factor text-height arrow-size precision
    tolerance-mode tolerance-upper tolerance-lower text)
)

(defun mcp-ad-vla-create-diameter
  (model-space item layer dimstyle scale-factor text-height arrow-size precision
   tolerance-mode tolerance-upper tolerance-lower
   / radial label-point dimension text)
  (setq label-point (nth 2 item)
        text (nth 3 item)
        radial (mcp-ad-vla-radial-data (nth 1 item) label-point))
  (setq dimension
    (vla-AddDimDiametric
      model-space
      (mcp-ad-vla-point (nth 2 radial))
      (mcp-ad-vla-point (nth 3 radial))
      (nth 4 radial)))
  (mcp-ad-vla-require-put dimension 'TextPosition
    (mcp-ad-vla-point label-point)
    "Could not place diameter dimension text.")
  (mcp-ad-vla-configure-dimension
    dimension layer dimstyle scale-factor text-height arrow-size precision
    tolerance-mode tolerance-upper tolerance-lower text)
)

(defun mcp-ad-vla-create-radius
  (model-space item layer dimstyle scale-factor text-height arrow-size precision
   tolerance-mode tolerance-upper tolerance-lower
   / radial label-point dimension text)
  (setq label-point (nth 2 item)
        text (nth 3 item)
        radial (mcp-ad-vla-radial-data (nth 1 item) label-point))
  (setq dimension
    (vla-AddDimRadial
      model-space
      (mcp-ad-vla-point (nth 0 radial))
      (mcp-ad-vla-point (nth 2 radial))
      (nth 4 radial)))
  (mcp-ad-vla-require-put dimension 'TextPosition
    (mcp-ad-vla-point label-point)
    "Could not place radial dimension text.")
  (mcp-ad-vla-configure-dimension
    dimension layer dimstyle scale-factor text-height arrow-size precision
    tolerance-mode tolerance-upper tolerance-lower text)
)

(defun mcp-ad-vla-create-center
  (model-space item layer fallback-size
   / entity data center radius requested-size size horizontal vertical)
  (setq entity (handent (nth 1 item)))
  (if (not entity)
    (error (strcat "Center-mark source handle was not found: " (nth 1 item))))
  (setq data (entget entity)
        center (cdr (assoc 10 data))
        radius (cdr (assoc 40 data))
        requested-size (nth 2 item))
  (if (or (not center) (not radius))
    (error "Center-mark source has invalid radial geometry."))
  (setq size
    (if (and requested-size (numberp requested-size) (> requested-size 0.0))
      requested-size
      (max fallback-size (* radius 0.15) 0.001)))
  (setq horizontal
    (vla-AddLine
      model-space
      (mcp-ad-vla-point
        (list (- (car center) size) (cadr center) 0.0))
      (mcp-ad-vla-point
        (list (+ (car center) size) (cadr center) 0.0))))
  (setq vertical
    (vla-AddLine
      model-space
      (mcp-ad-vla-point
        (list (car center) (- (cadr center) size) 0.0))
      (mcp-ad-vla-point
        (list (car center) (+ (cadr center) size) 0.0))))
  (mcp-ad-vla-require-put horizontal 'Layer layer
    "Could not set center-mark layer.")
  (mcp-ad-vla-require-put vertical 'Layer layer
    "Could not set center-mark layer.")
  (list horizontal vertical)
)

(defun mcp-ad-vla-create-text
  (model-space item layer text-height / entity)
  (setq entity
    (vla-AddText
      model-space
      (nth 2 item)
      (mcp-ad-vla-point (nth 1 item))
      text-height))
  (mcp-ad-vla-require-put entity 'Layer layer
    "Could not set annotation text layer.")
  entity
)

(defun mcp-ad-vla-create-instruction
  (model-space item layer dimstyle scale-factor text-height arrow-size precision
   tolerance-mode tolerance-upper tolerance-lower / kind)
  (setq kind (nth 0 item))
  (cond
    ((= kind "linear")
      (mcp-ad-vla-create-linear
        model-space item layer dimstyle scale-factor text-height arrow-size precision
        tolerance-mode tolerance-upper tolerance-lower))
    ((= kind "diameter")
      (mcp-ad-vla-create-diameter
        model-space item layer dimstyle scale-factor text-height arrow-size precision
        tolerance-mode tolerance-upper tolerance-lower))
    ((= kind "radius")
      (mcp-ad-vla-create-radius
        model-space item layer dimstyle scale-factor text-height arrow-size precision
        tolerance-mode tolerance-upper tolerance-lower))
    ((= kind "center")
      (mcp-ad-vla-create-center model-space item layer text-height))
    ((= kind "text")
      (mcp-ad-vla-create-text model-space item layer text-height))
    (t (error (strcat "Unsupported dimension instruction: " kind))))
)

(defun mcp-ad-vla-commit-instructions
  (model-space instructions layer dimstyle scale-factor text-height arrow-size precision
   tolerance-mode tolerance-upper tolerance-lower
   / item result created failed first-error)
  (setq created 0 failed 0 first-error nil)
  (foreach item instructions
    (setq result
      (vl-catch-all-apply
        'mcp-ad-vla-create-instruction
        (list model-space item layer dimstyle scale-factor text-height arrow-size precision
              tolerance-mode tolerance-upper tolerance-lower)))
    (if (vl-catch-all-error-p result)
      (progn
        (setq failed (1+ failed))
        (if (not first-error)
          (setq first-error (vl-catch-all-error-message result))))
      (setq created (1+ created))))
  (list created failed first-error)
)

(defun mcp-ad-write-activex-commit-report
  (report-file created failed / fp)
  (setq fp (open report-file "w"))
  (if fp
    (progn
      (write-line
        (strcat
          "{\"ok\":true,"
          "\"dimensions_created\":" (itoa created) ","
          "\"instructions_failed\":" (itoa failed) ","
          "\"commit_engine\":\"activex\","
          "\"regen_count\":1,"
          "\"undo_group\":\"single\"}")
        fp)
      (close fp)))
)

(defun mcp-commit-dimension-plan-file
  (plan-file report-file dim-layer clear-existing dimstyle scale-factor text-height arrow-size precision
   tolerance-mode tolerance-upper tolerance-lower
   / instructions application document model-space result counts undo-open end-result)
  "Commit a server-generated plan with ActiveX entity creation and one regen."
  (setq instructions (mcp-ad-read-plan-file plan-file))
  (if (not instructions)
    (progn
      (mcp-ad-write-error report-file "Dimension plan is empty or unreadable.")
      nil)
    (progn
      (setq application (vlax-get-acad-object)
            document (vla-get-ActiveDocument application)
            model-space (vla-get-ModelSpace document)
            undo-open nil
            counts nil)
      (setq result
        (vl-catch-all-apply
          '(lambda ()
            (vla-StartUndoMark document)
            (setq undo-open T)
            (if (not (mcp-ad-vla-ensure-layer document dim-layer))
              (error "Could not create or access the dimension layer."))
            (if (and dimstyle (> (strlen dimstyle) 0)
                     (not (mcp-ad-vla-ensure-dimstyle document dimstyle)))
              (error "Could not create or access the dimension style."))
            (if clear-existing
              (mcp-ad-vla-clear-generated-layer dim-layer))
            (setq counts
              (mcp-ad-vla-commit-instructions
                model-space instructions dim-layer dimstyle scale-factor text-height
                arrow-size precision tolerance-mode tolerance-upper tolerance-lower))
            (if (> (nth 1 counts) 0)
              (error
                (strcat
                  "Dimension plan failed: "
                  (if (nth 2 counts) (nth 2 counts) "unknown ActiveX error"))))
            ;; One redraw after the complete batch replaces per-command updates.
            (vla-Regen document 0)
            counts)))
      (if undo-open
        (progn
          (setq end-result
            (vl-catch-all-apply 'vla-EndUndoMark (list document)))
          (setq undo-open nil)
          (if (vl-catch-all-error-p end-result)
            (setq result end-result))))
      (if (vl-catch-all-error-p result)
        (progn
          ;; Roll back the closed mark as one operation. This command is used only
          ;; on failure; normal commits never start the AutoCAD command engine.
          (vl-catch-all-apply
            'command
            (list "_.UNDO" "1"))
          (mcp-ad-write-error report-file (vl-catch-all-error-message result))
          nil)
        (progn
          (mcp-ad-write-activex-commit-report
            report-file (nth 0 counts) (nth 1 counts))
          T))))
)

(princ)
