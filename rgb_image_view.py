"""RGB image import and preview of independently synthesized color/shift designs."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from destination_assignment import choose_destination_wells, assign_destination_wells
from app_state import occupied_destination_wells, all_384_wells, create_run_output_directory, record_transfers, save_json
from image_encoding import (
    CHANNELS, FIT_MODES, ROWS, IMAGE_COLUMNS, active_columns, default_barcode_schema, validate_barcode_schema, fiducial_sites, DEFAULT_SCHEME, ENCODING_SCHEMES, SELECTABLE_SCHEMES, TILE_COUNT_SCHEMES, SEVEN_BIT_TILE_SCHEMES, MAX_TILES_PER_COLOR, MAX_SEVEN_BIT_TILES, physical_raster_size,
    bin_image, compile_designs, geometry, prepare_runs, synthesis_base_volume_nl, rgb_replacement_options, reconstruct, site_name, rotate_point, physical_reference_raster, best_image_rotation, ranked_schemes, scheme_pixel_summary, schemes_for_bits, direct_tile_schemes,
)
from gaussian_reconstruction import DEFAULT_FWHM_NM, DEFAULT_EXPOSURE, render_gaussian_overlay
from gaussian_optimization import fitted_target, optimize_designs
from optimization_regularization import DEFAULT_REGULARIZATION, validate_regularization
from alignment_reconstruction import AlignmentModel, AlignmentSettings, VALIDATION_SEED
from brightness_simulation import brightness_cases
from picklist_core import HINGE_TYPES, PICKLIST_COLUMNS, combine_picklists, parse_vector, write_csv


class RGBImageView(ttk.Frame):
    def __init__(self, parent, app, app_dir, storage_path, plate_path):
        super().__init__(parent, padding=10)
        self.app, self.app_dir = app, app_dir
        self.storage_path, self.plate_path = storage_path, plate_path
        # RGB targets belong to this workflow, not manual replacement tabs.
        targets = app.config.get("image_plate_targets_nm", {})
        self.color_views = {color: SimpleNamespace(
            desired_concentration_nm=tk.StringVar(value=str(targets.get(color, 50))),
            path_var=tk.StringVar(value=str(Path(app_dir) / "replacement_sheets" / "PAINT_R1_replace.csv")),
            plate_name="SourcePlate7[7]", concentration_nm=20000.0,
        ) for color in CHANNELS}
        # RGB settings are persisted separately; never inherit the manual-run form.
        self.base_plate_options = dict(app.base_plate_options)
        defaults = {
            "base_plate": next(iter(self.base_plate_options)),
            "minimum_transfer_nl": "25", "transfer_increment_nl": "25",
            "final_volume": "100", "base_target": "50", "biotin_target": "50", "scaffold_target": "10",
            "destination_plate": "Destination[1]", "source_plate_type": "384PP_AQ_BP",
            "max_volume": "12.5", "hinge_type": HINGE_TYPES["original"],
            "output_root": "generated_output", "start_well": "A01",
            "base_path": str(Path(app_dir) / "replacement_sheets" / "book_base.csv"),
            "base_stock": "200000", "staple_buffer": "1,0,0",
            "scaffold": "400,0.1,0,0", "magnesium": "0,0,100,0",
            "sodium": "0,0,0,100", "te_10x": "0,10,0,0",
            "desired_buffer": "1,12,5",
            "replacement_path": str(Path(app_dir) / "replacement_sheets" / "PAINT_R1_replace.csv"),
            "replacement_plate": "SourcePlate7[7]", "replacement_stock": "20000",
            "biotin_path": str(Path(app_dir) / "replacement_sheets" / "lowconc_D_biotin_additions.csv"),
            "biotin_plate": "SourcePlate8[8]", "biotin_stock": "20000",
        }
        saved = dict(app.config.get("image_run_settings", {}))
        # Preserve the previous biotin concentration once, then keep it independent.
        saved.setdefault("biotin_target", saved.get("base_target", defaults["base_target"]))
        # Preserve explicitly saved RGB recipe inputs; old transfer/repeat inputs are retired.
        legacy_mix = str(saved.get("desired_mix", "")).split(",")
        if len(legacy_mix) == 5:
            for key, value in (("final_volume", legacy_mix[0]), ("scaffold_target", legacy_mix[1]),
                               ("desired_buffer", ",".join(legacy_mix[2:]))):
                saved.setdefault(key, value)
        self.run_variables = {key: tk.StringVar(value=str(saved.get(key, value))) for key, value in defaults.items()}
        for key, variable in self.run_variables.items():
            setattr(self, key, variable)
        if self.base_plate.get() not in self.base_plate_options:
            self.base_plate.set(defaults["base_plate"])
        self.transfer_volume = tk.StringVar()
        self.synthesis_summary = tk.StringVar()
        self._update_base_volume()
        self._sync_replacement_source()
        self.pixels = None
        self.comparison_window = None
        self.tiling_window = None
        self.designs, self.runs = [], []
        self.saved = self.stored = self.committed = False
        self.image_path = tk.StringVar()
        self.fit_mode = tk.StringVar(value=FIT_MODES[0])
        self.rotation = tk.StringVar(value="0")
        self.gaussian_fwhm = tk.StringVar(value=str(app.config.get("image_gaussian_fwhm_nm", DEFAULT_FWHM_NM)))
        self.gaussian_exposure = tk.StringVar(value=str(app.config.get("image_gaussian_exposure", DEFAULT_EXPOSURE)))
        self.linear_light = tk.BooleanVar(value=app.config.get("image_linear_light", True))
        self.normalize_designs = tk.BooleanVar(value=app.config.get("image_normalize_designs", True))
        self.compensate_area = tk.BooleanVar(value=app.config.get("image_compensate_area", True))
        self.reconstruction = None
        self.reconstruction_summary = tk.StringVar()
        self.optimize_enabled = tk.BooleanVar(value=True)
        self.regularization = tk.StringVar(value=str(app.config.get("image_optimization_regularization", DEFAULT_REGULARIZATION)))
        self.optimization_metrics = None
        self.optimization_summary = tk.StringVar()
        self.alignment_enabled = tk.BooleanVar(value=True)
        self.alignment_values = {key: tk.StringVar(value=str(value)) for key, value in vars(AlignmentSettings()).items()}
        self.brightness_enabled = tk.BooleanVar(value=False)
        self.brightness_values = {key: tk.StringVar(value=value) for key,value in
            (("ratio","1.10"),("cv_percent","10"),("transfer_cv_percent","10"),("seed","42"))}
        self.brightness_summary = tk.StringVar()
        self.brightness_results = []
        self.alignment_models = {}
        self.example_seed = 42
        self.alignment_example = None
        self.ideal_reconstruction = None
        self.alignment_summary = tk.StringVar()
        self.barcode_experiment = tk.StringVar(value=app.config.get("barcode_experiment", "Default"))
        saved_scheme = app.config.get("image_encoding_scheme", DEFAULT_SCHEME)
        if saved_scheme not in SELECTABLE_SCHEMES:
            saved_scheme = DEFAULT_SCHEME
        self.barcode_bit_count = tk.StringVar(value=str(SELECTABLE_SCHEMES[saved_scheme].bit_count))
        self.scheme = tk.StringVar(value=SELECTABLE_SCHEMES[saved_scheme].label)
        self.tile_count = tk.StringVar()
        self.tile_layout = tk.StringVar()
        try:
            saved_canvas = active_columns(app.config.get("image_active_columns"))
        except (ValueError, TypeError):
            saved_canvas = IMAGE_COLUMNS[:]
        self.active_row_vars = {c: tk.BooleanVar(value=c in saved_canvas) for c in IMAGE_COLUMNS}
        saved_barcode = app.config.get("image_barcode_schema")
        try:
            self.barcode_schema = default_barcode_schema() if saved_barcode is None else validate_barcode_schema(saved_barcode)
            barcode_status = self.barcode_schema["name"] + " · {} always-on sites".format(len(self.barcode_schema["always_on_sites"]))
        except ValueError as exc:
            self.barcode_schema = default_barcode_schema()
            barcode_status = "Saved schema invalid; using standard sites: {}".format(exc)
        self.barcode_schema_status = tk.StringVar(value=barcode_status)
        self.image_geometry = geometry(saved_scheme, active_image_columns=saved_canvas, barcode_schema=self.barcode_schema, validate_overlaps=False)
        self.geometry_summary = tk.StringVar()
        self.explanation = tk.StringVar()
        self.auto_export_templates = tk.BooleanVar(value=app.config.get("image_auto_export_templates", True))
        self.alignment_template_directory = None
        self.design_name = tk.StringVar()
        self.status = tk.StringVar(value="Upload an image to preview its color/shift designs.")
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Upload RGB image…", command=self._upload).pack(side="left")
        ttk.Label(toolbar, textvariable=self.image_path, width=55).pack(side="left", padx=10)
        fits = ttk.Combobox(toolbar, textvariable=self.fit_mode, values=FIT_MODES, state="readonly", width=24)
        fits.pack(side="right")
        fits.bind("<<ComboboxSelected>>", lambda _event: self._load())
        scheme_bar = ttk.Frame(self)
        scheme_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(scheme_bar, text="Barcode bits").pack(side="left")
        self.bit_count_picker = ttk.Combobox(scheme_bar, textvariable=self.barcode_bit_count,
                                             values=("6", "7", "9"), state="readonly", width=4)
        self.bit_count_picker.pack(side="left", padx=8)
        self.bit_count_picker.bind("<<ComboboxSelected>>", self._bit_count_changed)
        ttk.Label(scheme_bar, text="Encoding scheme").pack(side="left")
        self.scheme_choice = tk.StringVar()
        self.scheme_picker = ttk.Combobox(scheme_bar, textvariable=self.scheme_choice,
                                          state="readonly", width=85)
        self.scheme_picker.pack(side="left", padx=8)
        self.scheme_picker.bind("<<ComboboxSelected>>", self._scheme_choice_changed)
        ttk.Button(scheme_bar, text="Compare original schemes…", command=self._compare_schemes).pack(side="left", padx=4)
        ttk.Button(scheme_bar, text="Compare tiling & FWHM…", command=self._compare_tiling).pack(side="left", padx=4)
        ttk.Label(self, text="Schemes and grids: most estimated usable image pixels first (padding excluded, best orientation). "
                  "Uses a square image until upload.").pack(anchor="w")
        tile_bar = ttk.Frame(self)
        tile_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(tile_bar, text="Tiles per color").pack(side="left")
        self.tile_count_picker = ttk.Combobox(tile_bar, textvariable=self.tile_count,
            values=("Use encoding scheme", *map(str, range(1, MAX_TILES_PER_COLOR + 1))), state="readonly", width=20)
        self.tile_count_picker.pack(side="left", padx=8)
        self.tile_count_picker.bind("<<ComboboxSelected>>", self._tile_count_changed)
        ttk.Label(tile_bar, text="Grid (columns × rows)").pack(side="left")
        self.tile_layout_picker = ttk.Combobox(tile_bar, textvariable=self.tile_layout,
            state="readonly", width=10)
        self.tile_layout_picker.pack(side="left", padx=8)
        self.tile_layout_picker.bind("<<ComboboxSelected>>", self._tile_layout_changed)
        self.pixel_capacity_summary = tk.StringVar()
        ttk.Label(self, textvariable=self.pixel_capacity_summary).pack(anchor="w")
        self.exhaustive_pdf_button = ttk.Button(self, text="Export exhaustive PDF…", command=self._export_exhaustive_pdf)
        self.exhaustive_pdf_button.pack(anchor="w", pady=(6, 0))
        self.similarity_graph_button = ttk.Button(self, text="Export similarity graph…", command=self._export_similarity_graph)
        self.similarity_graph_button.pack(anchor="w", pady=(4, 0))
        self.similarity_optimization_button = ttk.Button(self, text="Run similarity optimization",
            command=lambda: self._export_exhaustive_pdf(similarity=True, robustness=True))
        self.similarity_optimization_button.pack(anchor="w", pady=(4, 0))
        rotation_bar = ttk.Frame(self)
        rotation_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(rotation_bar, text="Origami rotation (degrees clockwise)").pack(side="left")
        rotation_picker = ttk.Combobox(rotation_bar, textvariable=self.rotation,
                                      values=("0", "90", "180", "270"), state="readonly", width=5)
        rotation_picker.pack(side="left", padx=8)
        rotation_picker.bind("<<ComboboxSelected>>", lambda _event: self._load())
        ttk.Button(rotation_bar, text="Origami left 90°", command=lambda: self._rotate(-90)).pack(side="left", padx=4)
        ttk.Button(rotation_bar, text="Origami right 90°", command=lambda: self._rotate(90)).pack(side="left", padx=4)
        ttk.Button(rotation_bar, text="Best fit", command=lambda: self._load(auto_rotate=True)).pack(side="left", padx=4)
        ttk.Label(rotation_bar, text="Image stays upright; uploads and layout changes choose the best fit.").pack(side="left", padx=10)
        ttk.Label(self, textvariable=self.geometry_summary).pack(anchor="w", pady=6)
        barcode_bar = ttk.LabelFrame(self, text="Barcode site schema (12–07)", padding=5)
        barcode_bar.pack(fill="x", pady=(0, 6))
        ttk.Button(barcode_bar, text="Edit in Origami Templates…", command=self._edit_barcode_template).pack(side="left", padx=(0, 6))
        ttk.Button(barcode_bar, text="Use Origami Templates", command=self._use_barcode_template).pack(side="left", padx=(0, 6))
        ttk.Button(barcode_bar, text="Load schema…", command=self._load_barcode_schema).pack(side="left")
        ttk.Button(barcode_bar, text="Save schema…", command=self._save_barcode_schema).pack(side="left", padx=6)
        ttk.Button(barcode_bar, text="Reset to standard", command=lambda: self._apply_barcode_schema(default_barcode_schema())).pack(side="left")
        ttk.Label(barcode_bar, textvariable=self.barcode_schema_status, wraplength=500).pack(side="left", padx=10)
        canvas_bar = ttk.Frame(self)
        canvas_bar.pack(fill="x", pady=(0, 6))
        ttk.Label(canvas_bar, text="Active analog rows").pack(side="left", padx=(0, 8))
        for column, variable in self.active_row_vars.items():
            ttk.Checkbutton(canvas_bar, text=column, variable=variable,
                command=lambda c=column: self._active_canvas_changed(c)).pack(side="left", padx=3)
        self.canvas_summary = tk.StringVar()
        ttk.Label(canvas_bar, textvariable=self.canvas_summary).pack(side="left", padx=12)
        source_bar = ttk.Frame(self)
        source_bar.pack(fill="x", pady=(0, 6))
        ttk.Label(source_bar, text="Base plate stock").pack(side="left")
        self.base_plate_picker = ttk.Combobox(source_bar, textvariable=self.base_plate,
            values=tuple(self.base_plate_options), state="readonly", width=36)
        self.base_plate_picker.pack(side="left", padx=8)
        synthesis = ttk.LabelFrame(self, text="Synthesis per design")
        synthesis.pack(fill="x", pady=5)
        for index, (label, key) in enumerate((("Final volume (µL)", "final_volume"),
                ("Book-base staple target (nM)", "base_target"), ("Biotin target (nM)", "biotin_target"),
                ("Scaffold target (nM)", "scaffold_target"))):
            ttk.Label(synthesis, text=label).grid(row=0, column=2*index, padx=5, sticky="w")
            ttk.Entry(synthesis, textvariable=self.run_variables[key], width=9,
                      state="normal").grid(row=0, column=2*index+1, padx=5)
        ttk.Label(synthesis, text="Calculated base transfer (nL/staple)").grid(row=1, column=0, columnspan=2, sticky="w", padx=5)
        ttk.Entry(synthesis, textvariable=self.transfer_volume, state="readonly", width=10).grid(row=1, column=2, sticky="w", padx=5)
        for column, (label, key) in enumerate((("Minimum transfer (nL)", "minimum_transfer_nl"),
                                               ("Transfer increment (nL)", "transfer_increment_nl"))):
            ttk.Label(synthesis, text=label).grid(row=3, column=column*2, sticky="w", padx=5)
            ttk.Entry(synthesis, textvariable=self.run_variables[key], width=9).grid(row=3, column=column*2+1, padx=5)
        ttk.Label(synthesis, textvariable=self.synthesis_summary, wraplength=700).grid(row=2, column=0, columnspan=6, sticky="w", padx=5)
        settings = ttk.Frame(self)
        settings.pack(fill="x")
        self.replacement_options = {}
        for color, view in self.color_views.items():
            group = ttk.Frame(settings)
            group.pack(side="left", fill="both", expand=True, padx=6)
            ttk.Label(group, text=color + " site target (nM)").grid(row=0, column=0, sticky="w")
            ttk.Entry(group, textvariable=view.desired_concentration_nm, width=6).grid(row=0, column=1, padx=6)
            self.replacement_options[color] = tk.StringVar()
            ttk.Label(group, textvariable=self.replacement_options[color], wraplength=330).grid(
                row=1, column=0, columnspan=2, sticky="w", pady=3)
        self._update_replacement_options()
        self._build_run_settings()
        ttk.Label(self, text="Every RGB run includes 8 D-Biotin staples from the LowConc base plate at the independent biotin target. "
                  "Load P17–P24 at 20 µM for the default map; source settings are under Sources & mixing….",
                  wraplength=1050).pack(anchor="w", pady=3)
        reconstruction_bar = ttk.Frame(self)
        reconstruction_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(reconstruction_bar, text="Gaussian FWHM (nm)").pack(side="left")
        ttk.Spinbox(reconstruction_bar, from_=0.2, to=100, increment=0.5,
                    textvariable=self.gaussian_fwhm, width=6).pack(side="left", padx=8)
        ttk.Label(reconstruction_bar, text="Exposure (shared across schemes)").pack(side="left")
        ttk.Spinbox(reconstruction_bar, from_=0.05, to=20, increment=0.1,
                    textvariable=self.gaussian_exposure, width=6).pack(side="left", padx=8)
        ttk.Label(reconstruction_bar, textvariable=self.reconstruction_summary).pack(side="left", padx=8)
        light_bar = ttk.Frame(self)
        light_bar.pack(fill="x", pady=(6,0))
        ttk.Checkbutton(light_bar, text="Linear-light image processing (sRGB input/output)",
                        variable=self.linear_light, command=self._light_changed).pack(side="left")
        ttk.Checkbutton(light_bar, text="Equal total signal across encoding schemes",
                        variable=self.normalize_designs, command=self._light_changed).pack(side="left", padx=12)
        ttk.Checkbutton(self, text="Match brightness for larger canvases (exposure compensation by tile count)",
                        variable=self.compensate_area, command=self._light_changed).pack(anchor="w")
        alignment_bar = ttk.LabelFrame(self, text="Residual alignment error (after fiducial alignment, before decoded XY shift)", padding=5)
        alignment_bar.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(alignment_bar, text="Simulate alignment uncertainty", variable=self.alignment_enabled,
                        command=self._volumes_changed).grid(row=0, column=0, columnspan=2, sticky="w")
        fields = (("translation_x_nm", "X error SD (nm)"), ("translation_y_nm", "Y error SD (nm)"),
                  ("rotation_deg", "Angle SD (°)"), ("origami_per_design", "Origami / design"),
                  ("center_x_nm", "Fiducial center X (nm)"), ("center_y_nm", "Fiducial center Y (nm)"),
                  ("variability_penalty", "Extra variability penalty"))
        for index, (key, label) in enumerate(fields):
            row, column = 1 + index // 4, 2 * (index % 4)
            ttk.Label(alignment_bar, text=label).grid(row=row, column=column, sticky="w", padx=4)
            ttk.Entry(alignment_bar, textvariable=self.alignment_values[key], width=8).grid(row=row, column=column+1, padx=4)
        ttk.Label(alignment_bar, text="Illustrative errors; center is relative to C6 H01 before shifts. Default center: full staple grid.").grid(row=3, column=0, columnspan=8, sticky="w")
        brightness_bar = ttk.LabelFrame(self, text="R1/body competition error — preview only", padding=5)
        self.brightness_controls = brightness_bar
        ttk.Checkbutton(brightness_bar, text="Simulate stoichiometry errors", variable=self.brightness_enabled,
                        command=self._brightness_changed).grid(row=0,column=0,columnspan=2,sticky="w")
        for index,(key,label) in enumerate((("ratio","R1/body ratio multiplier (×)"),
                                            ("cv_percent","Source ratio CV (%)"),("transfer_cv_percent","Per-transfer volume CV (%)"),("seed","Random seed"))):
            ttk.Label(brightness_bar,text=label).grid(row=1,column=index*2,padx=4)
            ttk.Entry(brightness_bar,textvariable=self.brightness_values[key],width=7).grid(row=1,column=index*2+1)
        ttk.Label(brightness_bar,text="1.10 = 10% higher R1/body ratio; 0.90 = 10% lower. 0% stays off; 100% stays saturated.").grid(row=2,column=0,columnspan=6,sticky="w")
        ttk.Button(brightness_bar,text="New brightness example",command=self._new_brightness_example).grid(row=2,column=6,columnspan=2)
        ttk.Label(brightness_bar,text="R1 occupancy = g·p / (g·p + 1 − p), assuming equal binding effectiveness and brightness proportional to occupancy.\nSource ratios are log-normal (mean multiplier 1), shared across designs using the same staple. Transfer volumes have independent log-normal errors for R1 and body at each site/design.\nTransfer CV applies to total delivered volume per source/site/design, not each droplet. Set either CV to 0 to disable it. Image sites only; current alignment settings apply.\nPreview only: optimization, replacement percentages, and picklists are unchanged.",
                  wraplength=1050).grid(row=3,column=0,columnspan=8,sticky="w")
        optimization_bar = ttk.Frame(self)
        optimization_bar.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(optimization_bar, text="Optimize replacement percentages for Gaussian overlap",
                        variable=self.optimize_enabled, command=self._volumes_changed).pack(side="left")
        ttk.Label(optimization_bar, textvariable=self.optimization_summary).pack(side="left", padx=8)
        regularization_bar = ttk.Frame(self)
        regularization_bar.pack(fill="x", pady=(4, 0))
        ttk.Label(regularization_bar, text="Regularization strength").pack(side="left")
        ttk.Spinbox(regularization_bar, from_=0, to=10, increment=0.01,
                    textvariable=self.regularization, width=7).pack(side="left", padx=8)
        ttk.Label(regularization_bar, text="0 = off; higher values keep replacements closer to direct image assignments.").pack(side="left")
        ttk.Label(self, text="Optimization target: fitted uploaded image (linear light when enabled), not the binned preview.").pack(anchor="w", pady=(2, 0))
        previews = ttk.Frame(self)
        previews.pack(fill="x", pady=8)
        self.pixel_canvases = []
        for label in ("Binned RGB", "Predicted RGB — Gaussian overlay"):
            frame = ttk.LabelFrame(previews, text=label)
            frame.pack(side="left", padx=(0, 12))
            canvas = tk.Canvas(frame, width=300, height=214, background="#15191d", highlightthickness=0)
            canvas.pack()
            self.pixel_canvases.append(canvas)
            self.prediction_frame = frame
        self.baseline_frame = ttk.LabelFrame(previews, text="Before optimization — Gaussian overlay")
        self.baseline_canvas = tk.Canvas(self.baseline_frame, width=300, height=214, background="#15191d", highlightthickness=0)
        self.baseline_canvas.pack()
        explanation = ttk.Label(previews, justify="left", wraplength=310, textvariable=self.explanation)
        explanation.pack(side="left", padx=6, anchor="n")
        self.alignment_previews = ttk.LabelFrame(self, text="Alignment comparison — active replacement assignments", padding=5)
        self.alignment_canvases = []
        for label in ("Ideal alignment", "Expected alignment result (sampled mean)", "Finite-count example"):
            frame = ttk.LabelFrame(self.alignment_previews, text=label)
            frame.pack(side="left", padx=5)
            canvas = tk.Canvas(frame, width=300, height=214, background="#15191d", highlightthickness=0)
            canvas.pack()
            self.alignment_canvases.append(canvas)
        self.alignment_details = ttk.Frame(self)
        ttk.Label(self.alignment_details, textvariable=self.alignment_summary, wraplength=900).pack(side="left")
        ttk.Button(self.alignment_details, text="New alignment example", command=self._new_alignment_example).pack(side="right")
        brightness_bar.pack(fill="x", pady=(6,0))
        self.brightness_previews = ttk.LabelFrame(self,text="Brightness comparison — active replacement assignments",padding=5)
        self.brightness_canvases=[]
        for label in ("Nominal", "Systematic ratio", "Random source + transfer", "Systematic + random"):
            frame=ttk.LabelFrame(self.brightness_previews,text=label)
            frame.pack(side="left",padx=3)
            canvas=tk.Canvas(frame,width=225,height=214,background="#15191d",highlightthickness=0)
            canvas.pack()
            self.brightness_canvases.append(canvas)
        self.brightness_details=ttk.Label(self,textvariable=self.brightness_summary,wraplength=1000)
        chooser = ttk.Frame(self)
        self.alignment_pack_before = chooser
        chooser.pack(fill="x")
        ttk.Label(chooser, text="Inspect design").pack(side="left")
        self.design_picker = ttk.Combobox(chooser, textvariable=self.design_name, state="readonly", width=22)
        self.design_picker.pack(side="left", padx=8)
        self.design_picker.bind("<<ComboboxSelected>>", lambda _event: self._draw_design())
        self.design_summary = tk.StringVar()
        ttk.Label(chooser, textvariable=self.design_summary).pack(side="left")
        detail = ttk.Frame(self)
        detail.pack(fill="both", expand=True, pady=6)
        self.design_canvas = tk.Canvas(detail, width=590, height=235, background="#15191d", highlightthickness=0)
        self.design_canvas.pack(side="left", fill="y")
        inspector = self.design_inspector = ttk.Notebook(detail)
        inspector.pack(side="left", fill="both", expand=True)
        sites_page = ttk.Frame(inspector)
        picklist_page = ttk.Frame(inspector)
        recipe_page = ttk.Frame(inspector)
        inspector.add(sites_page, text="Image sites")
        inspector.add(picklist_page, text="Prepared picklist")
        inspector.add(recipe_page, text="Prepared mixing recipe")
        columns = ("Helix", "Row", "RGB", "Replacement %", "Repl. nL", "Original nL")
        self.site_tree = ttk.Treeview(sites_page, columns=columns, show="headings", height=8, selectmode="browse")
        for column in columns:
            self.site_tree.heading(column, text=column)
            self.site_tree.column(column, width=85, stretch=True)
        scroll = ttk.Scrollbar(sites_page, orient="vertical", command=self.site_tree.yview)
        self.site_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.site_tree.pack(side="left", fill="both", expand=True)
        self.picklist_summary = tk.StringVar(value="Prepare runs to see the actual transfers for this design.")
        ttk.Label(picklist_page, textvariable=self.picklist_summary, wraplength=540).pack(anchor="w")
        picklist_table = ttk.Frame(picklist_page)
        picklist_table.pack(fill="both", expand=True)
        self.picklist_columns = ("function", *PICKLIST_COLUMNS)
        self.picklist_tree = ttk.Treeview(picklist_table, columns=self.picklist_columns, show="headings", height=8, selectmode="browse")
        for column in self.picklist_columns:
            self.picklist_tree.heading(column, text="Transfer Volume (nL)" if column == "Transfer Volume" else column)
            self.picklist_tree.column(column, width=150, stretch=False)
        self.site_tree.bind("<<TreeviewSelect>>", self._highlight_selected_site)
        self.picklist_tree.bind("<<TreeviewSelect>>", self._highlight_selected_site)
        inspector.bind("<<NotebookTabChanged>>", self._highlight_selected_site)
        self.picklist_tree.column("function", width=370)
        vertical = ttk.Scrollbar(picklist_table, orient="vertical", command=self.picklist_tree.yview)
        horizontal = ttk.Scrollbar(picklist_table, orient="horizontal", command=self.picklist_tree.xview)
        self.picklist_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        picklist_table.rowconfigure(0, weight=1)
        picklist_table.columnconfigure(0, weight=1)
        self.picklist_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.recipe_summary = tk.StringVar(value="Prepare runs to see the mixing recipe for this design.")
        ttk.Label(recipe_page, textvariable=self.recipe_summary, wraplength=600).pack(anchor="w", pady=4)
        self.recipe_tree = ttk.Treeview(recipe_page, columns=("Reagent", "Volume (µL)"), show="headings", height=6)
        for column, width in (("Reagent", 250), ("Volume (µL)", 120)):
            self.recipe_tree.heading(column, text=column)
            self.recipe_tree.column(column, width=width)
        self.recipe_tree.pack(fill="x")
        ttk.Label(recipe_page, text="Staple mix collected per destination well (all staple types). "
                  "Pool the listed wells for this design before adding the recipe's other reagents.",
                  wraplength=600).pack(anchor="w", pady=4)
        well_frame = ttk.Frame(recipe_page)
        well_frame.pack(fill="both", expand=True)
        self.recipe_well_tree = ttk.Treeview(well_frame,
            columns=("Destination plate", "Well", "Staple mix (µL)"), show="headings", height=4)
        for column, width in (("Destination plate", 180), ("Well", 70), ("Staple mix (µL)", 130)):
            self.recipe_well_tree.heading(column, text=column)
            self.recipe_well_tree.column(column, width=width)
        well_scroll = ttk.Scrollbar(well_frame, orient="vertical", command=self.recipe_well_tree.yview)
        self.recipe_well_tree.configure(yscrollcommand=well_scroll.set)
        well_scroll.pack(side="right", fill="y")
        self.recipe_well_tree.pack(side="left", fill="both", expand=True)
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=6)
        ttk.Label(actions, text="Start at well").pack(side="left")
        ttk.Entry(actions, textvariable=self.start_well, width=6).pack(side="left", padx=6)
        self.prepare_button = ttk.Button(actions, command=self._prepare)
        self.prepare_button.pack(side="left", padx=6)
        self.export_button = ttk.Button(actions, text="Save image batch…", command=self._export, state="disabled")
        self.export_button.pack(side="left", padx=6)
        self.recipe_export_button = ttk.Button(actions, text="Save mixing recipe batch…",
                                               command=self._export_recipes, state="disabled")
        self.recipe_export_button.pack(side="left", padx=6)
        self.store_button = ttk.Button(actions, command=self._store, state="disabled")
        self.store_button.pack(side="left", padx=6)
        ttk.Label(self, textvariable=self.status, wraplength=1150).pack(anchor="w", pady=4)
        template_actions = ttk.Frame(self)
        template_actions.pack(fill="x", pady=4)
        ttk.Checkbutton(template_actions, text="Automatically export PAINT alignment templates when preparing runs",
            variable=self.auto_export_templates, command=self._save_template_export_preference).pack(side="left")
        self.open_templates_button = ttk.Button(template_actions, text="Open alignment templates",
            command=self._open_alignment_templates, state="disabled")
        self.open_templates_button.pack(side="left", padx=8)
        self._update_scheme_controls()
        self.regularization.trace_add("write", self._regularization_changed)
        self.scheme.trace_add("write", self._scheme_changed)
        self.gaussian_fwhm.trace_add("write", self._render_settings_changed)
        self.gaussian_exposure.trace_add("write", self._render_settings_changed)
        for variable in self.alignment_values.values():
            variable.trace_add("write", self._alignment_changed)
        for variable in self.brightness_values.values():
            variable.trace_add("write",self._brightness_changed)
        self.base_plate.trace_add("write", self._base_plate_changed)
        for key, variable in self.run_variables.items():
            if key != "base_plate":
                variable.trace_add("write", lambda *_args, key=key: self._run_setting_changed(key))
        for view in self.color_views.values():
            view.desired_concentration_nm.trace_add("write", self._volumes_changed)
            view.desired_concentration_nm.trace_add("write", self._save_rgb_targets)

    def _build_run_settings(self):
        frame = ttk.LabelFrame(self, text="RGB run settings (saved independently)")
        frame.pack(fill="x", pady=5)
        for index, (label, key) in enumerate((("Destination plate", "destination_plate"),
                ("Max well volume (µL)", "max_volume"))):
            ttk.Label(frame, text=label).grid(row=0, column=index*2, padx=5, sticky="w")
            ttk.Entry(frame, textvariable=self.run_variables[key], width=18).grid(row=0, column=index*2+1, padx=5)
        ttk.Label(frame, text="Experiment / pool").grid(row=1, column=0, padx=5, sticky="w")
        ttk.Entry(frame, textvariable=self.barcode_experiment, width=24).grid(row=1, column=1, padx=5, sticky="w")
        ttk.Label(frame, text="Use a new name for a separately decoded batch. A fresh plate keeps the same experiment.",
                  wraplength=650).grid(row=1, column=2, columnspan=5, padx=5, sticky="w")
        details = ttk.Notebook(frame)
        self.run_settings_details = details
        self.show_run_details = tk.BooleanVar(value=True)
        def toggle():
            if self.show_run_details.get():
                details.grid(row=2, column=0, columnspan=7, sticky="ew", pady=5)
            else:
                details.grid_remove()
        ttk.Checkbutton(frame, text="Sources & mixing…", variable=self.show_run_details,
                        command=toggle).grid(row=0, column=6, padx=5)
        toggle()
        pages = [
            ("Sources", [("Base source CSV", "base_path"), ("Base stock (nM)", "base_stock"),
                         ("RGB source CSV (all colors)", "replacement_path"),
                         ("RGB source plate name", "replacement_plate"), ("RGB stock (nM)", "replacement_stock"),
                         ("D-Biotin source map", "biotin_path"), ("D-Biotin source plate", "biotin_plate"),
                         ("D-Biotin stock (nM)", "biotin_stock"),
                         ("Source plate type", "source_plate_type"), ("Hinges", "hinge_type")]),
            ("Mixing", [("All staple stocks: buffer [×TE, Mg mM, Na mM]", "staple_buffer"),
                        ("Scaffold [nM, ×TE, Mg mM, Na mM]", "scaffold"),
                        ("Mg stock [nM, ×TE, Mg mM, Na mM]", "magnesium"),
                        ("Na stock [nM, ×TE, Mg mM, Na mM]", "sodium"),
                        ("TE stock [nM, ×TE, Mg mM, Na mM]", "te_10x"),
                        ("Final buffer [×TE, Mg mM, Na mM]", "desired_buffer")]),
            ("Output", [("Output folder", "output_root")]),
        ]
        for title, fields in pages:
            page = ttk.Frame(details, padding=5)
            details.add(page, text=title)
            page.columnconfigure(1, weight=1)
            for row, (label, key) in enumerate(fields):
                ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", padx=5)
                if key == "hinge_type":
                    entry = ttk.Combobox(page, textvariable=self.hinge_type, values=list(HINGE_TYPES.values()), state="readonly")
                else:
                    entry = ttk.Entry(page, textvariable=self.run_variables[key], width=55)
                entry.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
            if title == "Mixing":
                ttk.Label(page, text="Final volume and concentration targets are set above. Each transfer independently follows the minimum and increment. All dispensed staples go into that volume.").grid(
                    row=len(fields), column=0, columnspan=2, sticky="w", pady=4)

    def _save_run_settings(self):
        self.app.config["image_run_settings"] = {key: variable.get() for key, variable in self.run_variables.items()}
        self.app._schedule_config_save()

    def _sync_replacement_source(self):
        for view in self.color_views.values():
            view.path_var.set(self.replacement_path.get())
            view.plate_name = self.replacement_plate.get().strip()
            try:
                view.concentration_nm = float(self.replacement_stock.get())
            except ValueError:
                view.concentration_nm = float("nan")

    def _run_setting_changed(self, key):
        self._save_run_settings()
        if key.startswith("replacement_"):
            self._sync_replacement_source()
        if key in ("final_volume", "base_target", "base_stock", "replacement_stock", "minimum_transfer_nl", "transfer_increment_nl"):
            self._update_base_volume()
            self._volumes_changed()
        else:
            self._invalidate()

    def _base_plate_changed(self, *_args):
        filename, stock = self.base_plate_options[self.base_plate.get()]
        self.base_path.set(str(Path(self.app_dir) / "replacement_sheets" / filename))
        self.base_stock.set(str(stock))
        if filename == "book_base_20uM_interleaved.csv":
            self.hinge_type.set(HINGE_TYPES["balanced_rigid"])
        self._save_run_settings()

    def base_stock_concentration_nm(self):
        return float(self.base_stock.get())

    def _transfer_limits(self):
        return dict(minimum_transfer_nl=float(self.minimum_transfer_nl.get()),
                    transfer_increment_nl=float(self.transfer_increment_nl.get()))

    def _base_transfer_volume(self):
        return synthesis_base_volume_nl(float(self.final_volume.get()), self._base_concentration(),
                                        self.base_stock_concentration_nm(), **self._transfer_limits())

    def _base_concentration(self):
        return float(self.base_target.get())

    def _update_replacement_options(self):
        for color, view in self.color_views.items():
            try:
                grid = rgb_replacement_options(float(self.final_volume.get()), float(view.desired_concentration_nm.get()),
                                               self.base_stock_concentration_nm(), view.concentration_nm, **self._transfer_limits())
                fractions = grid["fractions"]
                levels = ["{:g}%".format(100 * fraction) for fraction in fractions]
                if len(levels) > 16:
                    levels = levels[:5] + ["…"] + levels[-5:]
                text = "Available: {} ({} levels).\n100% PAINT: {:g} nL; 0% original: {:g} nL. Minimum {:g} nL; increment {:g} nL.".format(
                    ", ".join(levels), len(fractions), grid["full_replacement_nl"], grid["full_original_nl"], float(self.minimum_transfer_nl.get()), float(self.transfer_increment_nl.get()))
                if not math.isclose(fractions[0], 0) or not math.isclose(fractions[-1], 1):
                    text += " RGB unavailable: both 0% and 100% must be achievable."
                self.replacement_options[color].set(text)
            except (ValueError, OverflowError) as exc:
                self.replacement_options[color].set("No available levels: {}".format(exc))

    def _update_base_volume(self):
        try:
            volume = self._base_transfer_volume()
        except ValueError as exc:
            self.transfer_volume.set("")
            self.synthesis_summary.set(str(exc))
        else:
            self.transfer_volume.set("{:g}".format(volume))
            self.synthesis_summary.set("Base transfer: {:g} nL per standard body staple. PAINT/competitor and biotin transfers "
                                       "follow their own targets, using the same minimum and increment.".format(volume))

    def _mixing_settings(self):
        vectors = {key: parse_vector(self.run_variables[key].get(), 4, key)
                   for key in ("scaffold", "magnesium", "sodium", "te_10x")}
        vectors["staple"] = [self.base_stock_concentration_nm(), *parse_vector(self.staple_buffer.get(), 3, "Staple buffer")]
        vectors["desired"] = [float(self.final_volume.get()), self._base_concentration(), float(self.scaffold_target.get()),
                              *parse_vector(self.desired_buffer.get(), 3, "Final buffer")]
        if any(not math.isfinite(value) or value < 0 for values in vectors.values() for value in values):
            raise ValueError("RGB mixing settings must contain finite, nonnegative values.")
        if any(value <= 0 for value in vectors["desired"][:2]):
            raise ValueError("Final volume and book-base target must be positive.")
        return vectors

    def _regularization_value(self):
        return validate_regularization(self.regularization.get())

    def _save_rgb_targets(self, *_args):
        try:
            targets = {color: float(view.desired_concentration_nm.get())
                       for color, view in self.color_views.items()}
            import math
            if not all(math.isfinite(value) and value > 0 for value in targets.values()):
                return
        except ValueError:
            return
        self.app.config["image_plate_targets_nm"] = targets
        self.app._schedule_config_save()

    def _regularization_changed(self, *_args):
        try:
            self.app.config["image_optimization_regularization"] = self._regularization_value()
            self.app._schedule_config_save()
        except ValueError:
            pass
        self._volumes_changed()

    def _render_settings_changed(self, *_args):
        self._volumes_changed()
        if self.reconstruction is not None:
            self.app.config["image_gaussian_fwhm_nm"] = float(self.gaussian_fwhm.get())
            self.app.config["image_gaussian_exposure"] = float(self.gaussian_exposure.get())
            self.app._schedule_config_save()

    def _light_changed(self):
        self.alignment_models.clear()
        self.app.config["image_compensate_area"] = self.compensate_area.get()
        self.app.config["image_linear_light"] = self.linear_light.get()
        self.app.config["image_normalize_designs"] = self.normalize_designs.get()
        self.app._schedule_config_save()
        self._load()

    def _light_options(self):
        return {"linear_light": self.linear_light.get(), "normalize_designs": self.normalize_designs.get(), "compensate_area": self.compensate_area.get()}

    def _new_brightness_example(self):
        try:
            self.brightness_values["seed"].set(str(int(self.brightness_values["seed"].get())+1))
        except ValueError:
            self.brightness_summary.set("Seed must be a nonnegative integer.")

    def _brightness_changed(self, *_args):
        self.brightness_previews.pack_forget()
        self.brightness_details.pack_forget()
        self.brightness_results=[]
        if not self.brightness_enabled.get() or not self.designs or self.reconstruction is None:
            return
        self.brightness_details.pack(fill="x",before=self.alignment_pack_before)
        try:
            settings={k:float(v.get()) for k,v in self.brightness_values.items() if k!="seed"}
            settings["seed"]=int(self.brightness_values["seed"].get())
            cases=brightness_cases(self.designs,**settings)
            self.brightness_results=[self._render_overlay(case) for case in cases]
            self.brightness_previews.pack(fill="x",pady=5,before=self.brightness_details)
            for canvas,result in zip(self.brightness_canvases,self.brightness_results):
                self._draw_overlay(canvas,result)
            self.brightness_summary.set("R1/body ratio {}×; source ratio CV {}%; per-transfer volume CV {}%; seed {}. "
                "Source errors shared across designs; transfers independent. Same draws in both random panels. {}".format(settings["ratio"],settings["cv_percent"],settings["transfer_cv_percent"],settings["seed"],
                "Includes expected alignment uncertainty." if self.alignment_enabled.get() else "Ideal alignment."))
        except ValueError as exc:
            self.brightness_summary.set("Brightness settings: {}".format(exc))

    def _alignment_settings(self):
        values = {key: float(var.get()) for key, var in self.alignment_values.items()}
        settings = AlignmentSettings(**values)
        settings.validate()
        values["origami_per_design"] = int(values["origami_per_design"])
        return AlignmentSettings(**values)

    def _alignment_changed(self, *_args):
        self.alignment_models.clear()
        if self.alignment_enabled.get():
            self._volumes_changed()

    def _alignment_model(self, designs, fwhm_nm=None):
        settings = self._alignment_settings()
        key = (designs[0]["encoding_scheme"], designs[0]["origami_rotation_degrees_clockwise"],
               settings, float(self.gaussian_fwhm.get()) if fwhm_nm is None else float(fwhm_nm), float(self.gaussian_exposure.get()),
               self.linear_light.get(), self.normalize_designs.get(), self.compensate_area.get(),
               tuple(designs[0].get("active_image_columns", IMAGE_COLUMNS)))
        if key not in self.alignment_models:
            if len(self.alignment_models) >= 2:
                self.alignment_models.clear()
            self.alignment_models[key] = AlignmentModel(designs, settings, key[3], key[4], **self._light_options())
        return self.alignment_models[key]

    def _new_alignment_example(self):
        if not self.alignment_enabled.get() or not self.designs or self.reconstruction is None:
            return
        self.example_seed += 1
        self.alignment_example = self._alignment_model(self.designs).example(self.designs, self.example_seed)
        self._draw_overlay(self.alignment_canvases[2], self.alignment_example)
        # The exported realization and its seed must match a newly prepared snapshot.
        self._invalidate()

    def _render_overlay(self, designs, fwhm_nm=None):
        fwhm_nm = float(self.gaussian_fwhm.get()) if fwhm_nm is None else float(fwhm_nm)
        if self.alignment_enabled.get():
            return self._alignment_model(designs, fwhm_nm).expected(designs)
        return render_gaussian_overlay(designs, fwhm_nm, float(self.gaussian_exposure.get()), **self._light_options())

    def _apply_barcode_schema(self, schema):
        value = validate_barcode_schema(schema)
        self.barcode_schema = value
        self.barcode_schema_status.set(value["name"] + " · {} always-on sites".format(len(value["always_on_sites"])))
        self.app.config["image_barcode_schema"] = deepcopy(value)
        self.app._schedule_config_save()
        self._update_scheme_controls()
        self._volumes_changed()

    def _edit_barcode_template(self):
        from barcode_template_schema import to_template
        editor = self.app.template_view
        model = to_template(self.barcode_schema, SELECTABLE_SCHEMES[self._scheme_id()].bit_count)
        editor.logical_schema = model
        editor.rows_var.set("8")
        editor.columns_var.set("12")
        editor.spacing_x_var.set("9.9")
        editor.spacing_y_var.set("5.1")
        editor.column_gap_var.set("10")
        editor.selected_group_id = None
        editor.selected_group_ids = []
        selected = {(row-1, col-1) for key in ("logical_bits", "alignment_groups")
                    for group in model[key] for row, col in group["physical_sites"]}
        editor._rebuild_grid(select_all=False, selected_sites=selected)
        editor.source_name.set("RGB barcode editor: name digital groups Bit 1, Bit 2, etc. Alignment-only groups are permanent fiducials. C1–C12 = 01–12; R1–R8 = H01,H03,…,H15.")
        editor.status.set("Edit site groups, then click Apply to RGB Image. Template bit ON/OFF checkboxes do not change the RGB codebook.")
        self.app.main_tabs.select(self.app.template_page)

    def _use_barcode_template(self):
        try:
            editor = self.app.template_view
            editor._settings()
            self._apply_schema_payload(editor.logical_schema)
            self.app.main_tabs.select(self.app.rgb_image_page)
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Cannot use template schema", str(exc), parent=self)

    def _apply_schema_payload(self, payload):
        if isinstance(payload, dict) and ("logical_model" in payload or "physical_rows" in payload):
            from barcode_template_schema import from_template
            value, count = from_template(payload, self.barcode_schema)
            self.barcode_bit_count.set(str(count))
            self._bit_count_changed()
            self._apply_barcode_schema(value)
        else:
            self._apply_barcode_schema(payload)

    def _load_barcode_schema(self):
        path = filedialog.askopenfilename(parent=self, title="Load barcode site schema",
            filetypes=[("JSON schema", "*.json")])
        if path:
            try:
                self._apply_schema_payload(json.loads(Path(path).read_text(encoding="utf-8")))
            except (ValueError, TypeError, OSError) as exc:
                messagebox.showerror("Cannot load barcode schema", str(exc), parent=self)

    def _save_barcode_schema(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save barcode site schema",
            defaultextension=".json", initialfile="barcode_schema.json", filetypes=[("JSON schema", "*.json")])
        if path:
            try:
                Path(path).write_text(json.dumps(self.barcode_schema, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Cannot save barcode schema", str(exc), parent=self)

    def _active_columns(self):
        return active_columns([c for c, var in self.active_row_vars.items() if var.get()])

    def _active_canvas_changed(self, changed_column):
        if not any(var.get() for var in self.active_row_vars.values()):
            self.active_row_vars[changed_column].set(True)
            self.status.set("Keep at least one analog row active.")
            return
        self.app.config["image_active_columns"] = self._active_columns()
        self.app._schedule_config_save()
        self.alignment_models.clear()
        self._update_scheme_controls()
        self._invalidate()
        if self.image_path.get():
            self._load(auto_rotate=True)
        else:
            self._refresh_comparison()

    def _concentration_options(self):
        return dict(**self._transfer_limits(), barcode_schema=deepcopy(self.barcode_schema), active_image_columns=self._active_columns(), plate_defaults_nm={color: float(view.desired_concentration_nm.get())
                                      for color, view in self.color_views.items()},
                    plate_relative_transfers=False,
                    base_desired_concentration_nm=self._base_concentration(),
                    base_concentration_nm=self.base_stock_concentration_nm(),
                    source_concentrations_nm={color: view.concentration_nm for color, view in self.color_views.items()})

    def _compile_scheme(self, pixels, scheme_id):
        """Use identical concentration settings for the main view and comparison."""
        return compile_designs(
            pixels, self._base_transfer_volume(),
            {color: 1.0 for color in self.color_views},
            scheme_id, int(self.rotation.get()), linear_light=self.linear_light.get(), validate_overlaps=False,
            **self._concentration_options())

    def _optimize(self, designs, fwhm_nm=None):
        fwhm_nm = float(self.gaussian_fwhm.get()) if fwhm_nm is None else float(fwhm_nm)
        overlay = self._render_overlay(designs, fwhm_nm)
        target = fitted_target(self.image_path.get(), self.fit_mode.get(), overlay.rgb.size, linear_light=self.linear_light.get())
        if self.alignment_enabled.get():
            model = self._alignment_model(designs, fwhm_nm)
            result, metrics = model.optimize(designs, target, regularization_strength=self._regularization_value())
            validation = AlignmentModel(designs, model.settings, model.fwhm, model.exposure, seed=VALIDATION_SEED, **self._light_options())
            metrics["validation_seed"] = VALIDATION_SEED
            metrics["validation_baseline"] = validation.evaluate(designs, target)
            metrics["validation_optimized"] = validation.evaluate(result, target)
            metrics["validation_used_for_selection"] = False
            return result, metrics
        return optimize_designs(designs, target, fwhm_nm, float(self.gaussian_exposure.get()), **self._light_options(), regularization_strength=self._regularization_value())

    def _draw_overlay(self, canvas, result, info=None, draw_width=288):
        from PIL import Image, ImageTk
        info = self.image_geometry if info is None else info
        # Comparison panels have different widths. Fit to this canvas rather
        # than drawing a 288-pixel image into a 225-pixel brightness panel.
        canvas_width = float(canvas.cget("width"))
        available_width = max(1, min(draw_width, canvas_width - 12))
        scale = available_width / max(info["display_field_width_nm"], info["display_field_height_nm"])
        width = max(1, round(scale * info["display_field_width_nm"]))
        height = max(1, round(scale * info["display_field_height_nm"]))
        canvas.delete("all")
        canvas.configure(height=height + 14)
        canvas._overlay_photo = ImageTk.PhotoImage(result.rgb.resize((width, height), Image.Resampling.LANCZOS), master=canvas)
        canvas.create_image((canvas_width - width) / 2, 7, anchor="nw", image=canvas._overlay_photo)

    def _compare_schemes(self):
        if self.pixels is None:
            messagebox.showinfo("Image required", "Upload an image to compare the encoding schemes.", parent=self)
            return
        if self.comparison_window is None or not self.comparison_window.winfo_exists():
            self.comparison_window = EncodingComparison(self)
        else:
            self.comparison_window.lift()
        self.comparison_window.refresh()

    def _compare_tiling(self):
        if self.pixels is None:
            messagebox.showinfo("Image required", "Upload an image to compare tiling and FWHM.", parent=self)
            return
        if self.tiling_window is None or not self.tiling_window.winfo_exists():
            from tiling_comparison import TilingComparison
            self.tiling_window = TilingComparison(self)
            self.tiling_window.refresh()
        else:
            self.tiling_window.lift()
            if self.tiling_window.stale:
                self.tiling_window.refresh()

    def _refresh_comparison(self):
        if self.tiling_window is not None and self.tiling_window.winfo_exists():
            self.tiling_window.mark_stale()
        if self.comparison_window is not None and self.comparison_window.winfo_exists():
            self.comparison_window.refresh()

    def _scheme_id(self):
        return next(key for key, scheme in SELECTABLE_SCHEMES.items() if scheme.label == self.scheme.get())

    def _bit_count_changed(self, _event=None):
        count = int(self.barcode_bit_count.get())
        current = SELECTABLE_SCHEMES[self._scheme_id()]
        if current.bit_count == count:
            self._update_scheme_controls()
            return
        choices = schemes_for_bits(count)
        matching = [key for key, scheme in direct_tile_schemes(count).items()
                    if (scheme.tile_columns, scheme.tile_rows) == (current.tile_columns, current.tile_rows)
                    and current.x_states == current.tile_columns and current.y_states == current.tile_rows]
        key = matching[-1] if matching else next(iter(choices))
        self.scheme.set(choices[key].label)

    def _pixel_summary(self, key):
        return scheme_pixel_summary(key, getattr(self, "_ranking_image_size", (1, 1)),
                                    self.fit_mode.get(), self._active_columns())

    def _refresh_scheme_choices(self):
        self._display_scheme_ids = self._rank_scheme_ids()
        labels = [self._pixel_summary(key) + " · " + SELECTABLE_SCHEMES[key].label
                  for key in self._display_scheme_ids]
        self.scheme_picker.configure(values=labels)
        current = self._scheme_id()
        if current in self._display_scheme_ids:
            self.scheme_choice.set(labels[self._display_scheme_ids.index(current)])
        self.pixel_capacity_summary.set(self._pixel_summary(current) + " (usable estimate uses best orientation)")

    def _scheme_choice_changed(self, _event=None):
        index = self.scheme_picker.current()
        if index >= 0:
            self.scheme.set(SELECTABLE_SCHEMES[self._display_scheme_ids[index]].label)

    def _rank_scheme_ids(self, keys=None):
        allowed = schemes_for_bits(int(self.barcode_bit_count.get()))
        keys = allowed if keys is None else [key for key in keys if key in allowed]
        return ranked_schemes(keys, getattr(self, "_ranking_image_size", (1, 1)),
                              self.fit_mode.get(), self._active_columns())

    def _refresh_scheme_ranking(self):
        from PIL import Image, ImageOps
        with Image.open(self.image_path.get()) as source:
            self._ranking_image_size = ImageOps.exif_transpose(source).size
        self._refresh_scheme_choices()
        self._sync_tile_controls()

    def _tile_layouts(self, count, bit_count=None):
        if bit_count is None:
            bit_count = int(self.barcode_bit_count.get())
        schemes = direct_tile_schemes(bit_count)
        keys = self._rank_scheme_ids([key for key, s in schemes.items() if s.tile_count == count])
        return {"{} × {}".format(schemes[key].tile_columns, schemes[key].tile_rows): key for key in keys}

    def _tile_count_changed(self, _event=None):
        if self.tile_count.get() == "Use encoding scheme":
            self.scheme.set(next(iter(schemes_for_bits(int(self.barcode_bit_count.get())).values())).label)
            return
        layouts = self._tile_layouts(int(self.tile_count.get()))
        if not layouts:
            self._sync_tile_controls()
            messagebox.showerror("Tile count exceeds barcode capacity", "Choose a supported tile count or change Barcode bits first.", parent=self)
            return
        # Start with the most compact grid; either orientation remains selectable.
        key = min(layouts.values(), key=lambda k: (
            abs(SELECTABLE_SCHEMES[k].tile_columns - SELECTABLE_SCHEMES[k].tile_rows),
            -SELECTABLE_SCHEMES[k].tile_columns))
        self.scheme.set(SELECTABLE_SCHEMES[key].label)

    def _tile_layout_changed(self, _event=None):
        key = self._grid_layout_options[self.tile_layout.get()]
        self.scheme.set(SELECTABLE_SCHEMES[key].label)

    def _sync_tile_controls(self):
        key = self._scheme_id()
        scheme = SELECTABLE_SCHEMES[key]
        # Existing saved sweep layouts can also have one design per color per
        # tile. Expose their grid without forcing the user to reselect a count.
        direct_tiles = key in direct_tile_schemes(scheme.bit_count)
        if direct_tiles:
            self.tile_count.set(str(scheme.tile_count))
            layouts = self._tile_layouts(scheme.tile_count, scheme.bit_count)
        else:
            self.tile_count.set("Use encoding scheme")
            # Preserve the encoding capacity and barcode family when changing
            # an advanced layout's grid.
            layouts = {}
            for candidate_key, candidate in SELECTABLE_SCHEMES.items():
                if (candidate.tile_count == scheme.tile_count
                        and candidate.design_count == scheme.design_count
                        and candidate.bit_count == scheme.bit_count
                        and candidate.joint == scheme.joint
                        and candidate.parity == scheme.parity):
                    label = "{} × {}".format(candidate.tile_columns, candidate.tile_rows)
                    layouts.setdefault(label, candidate_key)
        current = "{} × {}".format(scheme.tile_columns, scheme.tile_rows)
        layouts[current] = key
        order = {key: index for index, key in enumerate(self._rank_scheme_ids(layouts.values()))}
        layouts = dict(sorted(layouts.items(), key=lambda item: order[item[1]]))
        self._grid_layout_options = layouts
        self.tile_layout_picker.configure(values=tuple(layouts), state="readonly")
        self.tile_layout.set(current)

    def _update_scheme_controls(self):
        counts = sorted({scheme.tile_count for scheme in direct_tile_schemes(int(self.barcode_bit_count.get())).values()})
        self.tile_count_picker.configure(values=("Use encoding scheme", *map(str, counts)))
        self._refresh_scheme_choices()
        self._sync_tile_controls()
        self.image_geometry = geometry(self._scheme_id(), int(self.rotation.get()), self._active_columns(), self.barcode_schema, validate_overlaps=False)
        self._last_valid_scheme = self._scheme_id()
        self.canvas_summary.set("8 × {} pixels per origami; barcode rows 12–07 unchanged".format(len(self._active_columns())))
        info = self.image_geometry
        self.geometry_summary.set(
            "Upright image: {display_columns} × {display_rows} pixels · {display_pixel_pitch_x_nm:.4g} × {display_pixel_pitch_y_nm:.4g} nm/pixel · "
            "{display_field_width_nm:g} × {display_field_height_nm:g} nm field · Origami {origami_rotation_degrees_clockwise}° clockwise".format(**info))
        if info.get("nonuniform_sampling"):
            self.geometry_summary.set(self.geometry_summary.get() + " · nonuniform spacing")
        self.prepare_button.configure(text="Prepare {} runs".format(info["design_count"]))
        self.store_button.configure(text="Send {} runs to storage".format(info["design_count"]))
        scheme = SELECTABLE_SCHEMES[self._scheme_id()]
        self.explanation.set(
            ("{} designs: 3 colors distributed across {} tiles.\n\n".format(scheme.design_count, scheme.tile_count) if scheme.tiled else "{design_count} designs: 3 colors × {x_states} X-shifts × {y_states} Y-shifts.\n\n".format(**info))
            + self._pixel_summary(self._scheme_id()) + "\n\n"
            + "Overlay: shifted Gaussian signals weighted by each site’s replacement percentage. Assumes equal design abundance and channel brightness; barcode and fiducial sites are excluded.")

    def _scheme_changed(self, *_args):
        if SELECTABLE_SCHEMES[self._scheme_id()].bit_count != int(self.barcode_bit_count.get()):
            self.scheme.set(SELECTABLE_SCHEMES[self._last_valid_scheme].label)
            messagebox.showerror("Different barcode bit count", "Choose the matching Barcode bits before selecting this scheme.", parent=self)
            return
        self._update_scheme_controls()
        self.app.config["image_encoding_scheme"] = self._scheme_id()
        self.app._schedule_config_save()
        self._invalidate()
        if self.image_path.get():
            self._load(auto_rotate=True)
        else:
            self.status.set("Upload an image to preview {} color/shift designs.".format(self.image_geometry["design_count"]))

    def _invalidate(self, *_args):
        self.alignment_template_directory = None
        if hasattr(self, "open_templates_button"):
            self.open_templates_button.configure(state="disabled")
        self.runs = []
        self._draw_picklist()
        self.saved = self.stored = self.committed = False
        self.export_button.configure(state="disabled")
        self.recipe_export_button.configure(state="disabled")
        self.store_button.configure(state="disabled")
        if self.pixels is not None:
            self.status.set("Preview ready. Prepare {} runs to allocate separate unused wells using the current settings.".format(self.image_geometry["design_count"]))

    def _upload(self):
        path = filedialog.askopenfilename(parent=self, title="Choose RGB image", filetypes=[
            ("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp"), ("All files", "*")])
        if path:
            self.image_path.set(path)
            self._load(auto_rotate=True)

    def _rotate(self, degrees):
        self.rotation.set(str((int(self.rotation.get()) + degrees) % 360))
        self._load()

    def _load(self, auto_rotate=False):
        if not self.image_path.get():
            return
        self._update_scheme_controls()
        self.pixels = None
        self.designs = []
        self.reconstruction = None
        self.brightness_previews.pack_forget()
        self.brightness_details.pack_forget()
        self.alignment_previews.pack_forget()
        self.alignment_details.pack_forget()
        self.alignment_example = self.ideal_reconstruction = None
        self._invalidate()
        try:
            self._refresh_scheme_ranking()
            if auto_rotate:
                self.rotation.set(str(best_image_rotation(self.image_path.get(), self._scheme_id(), self._active_columns())))
                self._update_scheme_controls()
            self.pixels = bin_image(self.image_path.get(), self.fit_mode.get(), int(self.rotation.get()), self._scheme_id(), linear_light=self.linear_light.get(), active_image_columns=self._active_columns())
            self._volumes_changed()
        except Exception as exc:
            self.status.set(str(exc))
            for canvas in (*self.pixel_canvases, self.design_canvas):
                canvas.delete("all")
            self.site_tree.delete(*self.site_tree.get_children())
            self._refresh_comparison()
            messagebox.showerror("Cannot import image", str(exc), parent=self)

    def _volumes_changed(self, *_args):
        self._update_replacement_options()
        self._invalidate()
        self.optimization_metrics = None
        self.optimization_summary.set("")
        self.baseline_frame.pack_forget()
        self.brightness_previews.pack_forget()
        self.brightness_details.pack_forget()
        self.alignment_previews.pack_forget()
        self.alignment_details.pack_forget()
        self.alignment_example = self.ideal_reconstruction = None
        self.reconstruction = None
        if self.pixels is None:
            return
        self._draw_pixels(self.pixel_canvases[0], self.pixels)
        try:
            self.designs = self._compile_scheme(self.pixels, self._scheme_id())
            if self.optimize_enabled.get():
                self.optimization_summary.set("Optimizing…")
                self.update_idletasks()
                baseline = self._render_overlay(self.designs)
                self.designs, self.optimization_metrics = self._optimize(self.designs)
                self.baseline_frame.pack(side="left", padx=(0, 12), before=self.prediction_frame)
                self._draw_overlay(self.baseline_canvas, baseline)
                metrics = self.optimization_metrics
                self.optimization_summary.set("RGB RMSE {:.4f} → {:.4f} · squared error reduced {:.1%}".format(
                    metrics["baseline_rmse"], metrics["optimized_rmse"], metrics["mse_reduction_fraction"]))
                if "validation_optimized" in metrics:
                    self.optimization_summary.set(self.optimization_summary.get() + " · held-out RMSE {:.4f} → {:.4f}".format(
                        metrics["validation_baseline"]["expected_mse"]**.5, metrics["validation_optimized"]["expected_mse"]**.5))
            self.design_picker.configure(values=[design["name"] for design in self.designs])
            if self.design_name.get() not in {design["name"] for design in self.designs}:
                self.design_name.set(self.designs[0]["name"])
            self.reconstruction = self._render_overlay(self.designs)
            self._draw_overlay(self.pixel_canvases[1], self.reconstruction)
            self.reconstruction_summary.set("{:.1%} of display pixels clipped".format(self.reconstruction.metadata["clipped_pixel_fraction"]))
            if self.alignment_enabled.get():
                self.prediction_frame.configure(text="Predicted RGB — expected alignment result")
                model = self._alignment_model(self.designs)
                self.ideal_reconstruction = render_gaussian_overlay(self.designs, model.fwhm, model.exposure, **self._light_options())
                self.alignment_example = model.example(self.designs, self.example_seed)
                self.alignment_previews.pack(fill="x", pady=5, before=self.brightness_controls)
                self.alignment_details.pack(fill="x", before=self.brightness_controls)
                for canvas, result in zip(self.alignment_canvases, (self.ideal_reconstruction, self.reconstruction, self.alignment_example)):
                    self._draw_overlay(canvas, result)
                self.alignment_summary.set("{} independent origami/design · zero-mean Gaussian rigid errors · 32 sampled transforms for expectation. "
                    "Fractional signals; example simulates alignment only, not occupancy or photon noise. Error axes rotate with the origami.".format(model.settings.origami_per_design))
            else:
                self.prediction_frame.configure(text="Predicted RGB — Gaussian overlay")
            area_gain = self.reconstruction.metadata.get("canvas_area_gain", 1)
            self.reconstruction_summary.set(self.reconstruction_summary.get() + " · canvas exposure compensation {:g}×".format(area_gain))
            self._draw_design()
            self._brightness_changed()
        except ValueError as exc:
            self.designs = []
            self.reconstruction = None
            self.baseline_frame.pack_forget()
            self.brightness_previews.pack_forget()
            self.brightness_details.pack_forget()
            self.alignment_previews.pack_forget()
            self.alignment_details.pack_forget()
            self.status.set("Check volume and reconstruction settings: {}".format(exc))
            self.reconstruction_summary.set(str(exc))
            self.pixel_canvases[1].delete("all")
            self.pixel_canvases[1].create_text(150, 20, anchor="n", width=275,
                fill="#ffd166", text="Preview unavailable\n\n{}".format(exc), tags="preview_error")
            self.design_picker.configure(values=())
            self.design_name.set("")
            self.design_summary.set("")
            self.design_canvas.delete("all")
            self.site_tree.delete(*self.site_tree.get_children())
        self._refresh_comparison()

    def _pixel_bounds(self, x, y, info=None, draw_width=288):
        info = self.image_geometry if info is None else info
        x1,x2 = info["pixel_edges_x_nm"][x:x+2]
        y1,y2 = info["pixel_edges_y_nm"][y:y+2]
        angle = info["origami_rotation_degrees_clockwise"]
        a = rotate_point(x1,y1,info["field_width_nm"],info["field_height_nm"],angle)
        b = rotate_point(x2,y2,info["field_width_nm"],info["field_height_nm"],angle)
        scale = draw_width / max(info["display_field_width_nm"], info["display_field_height_nm"])
        left = 6 + (draw_width - scale * info["display_field_width_nm"]) / 2
        return (left+min(a[0],b[0])*scale,7+min(a[1],b[1])*scale,
                left+max(a[0],b[0])*scale,7+max(a[1],b[1])*scale)


    def _draw_pixels(self, canvas, pixels, info=None, draw_width=288):
        info = self.image_geometry if info is None else info
        canvas.delete("all")
        canvas.configure(height=round(draw_width * info["display_field_height_nm"] / max(info["display_field_width_nm"], info["display_field_height_nm"]) + 14))
        for y, row in enumerate(pixels):
            for x, rgb in enumerate(row):
                canvas.create_rectangle(*self._pixel_bounds(x, y, info, draw_width),
                                        fill="#{:02x}{:02x}{:02x}".format(*rgb), outline="")
        # Tile boundaries depend on image geometry, not on valid transfer
        # volumes or a successfully compiled/selected design.
        scheme = SELECTABLE_SCHEMES[info["encoding_scheme"]]
        if scheme.tile_count > 1:
            y = 0
            for ny in scheme.y_counts:
                x = 0
                for nx in scheme.x_counts:
                    first = self._pixel_bounds(x, y, info, draw_width)
                    last = self._pixel_bounds(x + len(info["image_columns_left_to_right"]) * nx - 1, y + 8 * ny - 1, info, draw_width)
                    bounds = (min(first[0], last[0]), min(first[1], last[1]),
                              max(first[2], last[2]), max(first[3], last[3]))
                    canvas.create_rectangle(*bounds, outline="black", width=3, tags="tile_grid")
                    canvas.create_rectangle(*bounds, outline="#ffd166", width=1, tags="tile_grid")
                    x += len(info["image_columns_left_to_right"]) * nx
                y += 8 * ny

    def _draw_design(self):
        self._draw_picklist()
        if not self.designs:
            return
        design = next(item for item in self.designs if item["name"] == self.design_name.get())
        binned_canvas = self.pixel_canvases[0]
        binned_canvas.delete("design_pixels")
        for site in design["image_sites"]:
            bounds = self._pixel_bounds(site["pixel_x"], site["pixel_y"])
            # A black halo keeps the white outline visible against any RGB value.
            binned_canvas.create_rectangle(*bounds, fill="", outline="black", width=3, tags="design_pixels")
            binned_canvas.create_rectangle(*bounds, fill="", outline="white", width=1, tags="design_pixels")
        self.design_summary.set("Bits {} · composite placement +{:g} nm X, +{:g} nm Y".format(
            "".join(map(str, design["bits"])), design["x_shift_nm"], design["y_shift_nm"]))
        if design.get("always_on_sites"):
            self.design_summary.set(self.design_summary.get() + " · {} always-on fiducials (gold rings)".format(len(design["always_on_sites"])))
        if "quadrant" in design:
            self.design_summary.set(self.design_summary.get() + " · origami-frame {} · local shift ({}, {})".format(
                self.image_geometry["quadrant_order"][design["quadrant"]],
                design["local_x_shift"], design["local_y_shift"]))
        canvas = self.design_canvas
        canvas.delete("all")
        color = [0, 0, 0]
        color[design["channel"]] = 255
        rotation = int(self.rotation.get())
        portrait = rotation in (90, 270)
        canvas.configure(width=260 if portrait else 590, height=570 if portrait else 235)
        def position(x, y):
            px, py = rotate_point(x, y, 118.9, 35.7, rotation)
            return 55 + px * 4, 42 + py * 4
        for c in range(12):
            x_nm = c * 9.9 + (10 if c >= 6 else 0)
            label_x, label_y = position(x_nm, -6.5)
            canvas.create_text(label_x, label_y, text="{:02d}".format(12-c), fill="white")
            for r, row in enumerate(ROWS):
                x, y = position(x_nm, r * 5.1)
                if c == 0:
                    canvas.create_text(*position(-8, r * 5.1), text=row, fill="white")
                fraction = design["site_fractions"].get(site_name(row, "{:02d}".format(12-c)), 0)
                shade = "#{:02x}{:02x}{:02x}".format(*(round(bg*(1-fraction)+fg*fraction) for bg, fg in zip((21,25,29),color)))
                canvas.create_oval(x-8, y-8, x+8, y+8, fill=shade, outline="#748087",
                                   tags=("site_{}_{:02d}".format(row, 12-c),))
                if [row, "{:02d}".format(12-c)] in design.get("always_on_sites", []):
                    canvas.create_oval(x-10, y-10, x+10, y+10, outline="#ffd166", width=2, tags="fiducial_sites")
                elif "{:02d}".format(12-c) in IMAGE_COLUMNS and "{:02d}".format(12-c) not in design.get("active_image_columns", IMAGE_COLUMNS):
                    canvas.create_line(x-5, y+5, x+5, y-5, fill="#748087", width=2, tags="inactive_canvas")
        self.site_tree.delete(*self.site_tree.get_children())
        for site in design["image_sites"]:
            self.site_tree.insert("", "end", values=(
                site["row"], site["column"],
                ",".join(map(str, site["rgb"])), "{:.4g}".format(site["replacement_fraction"]*100),
                site["replacement_nl"], site["original_nl"]))

    def _highlight_selected_site(self, _event=None):
        canvas = self.design_canvas
        canvas.delete("selected_site")
        tab = self.design_inspector.index(self.design_inspector.select())
        if tab not in (0, 1):
            return
        tree = self.site_tree if tab == 0 else self.picklist_tree
        selection = tree.selection()
        if not selection:
            return
        values = tree.item(selection[0], "values")
        if tab == 0:
            helix, row = values[:2]
        else:
            location = self.picklist_site_locations.get(selection[0])
            if not location:
                canvas.create_text(float(canvas.cget("width"))/2, float(canvas.cget("height"))-12,
                                   text="Location unavailable for this staple", fill="#00e5ff",
                                   tags="selected_site")
                return
            helix, row = location
        items = canvas.find_withtag("site_{}_{}".format(helix, row))
        if not items:
            return
        left, top, right, bottom = canvas.coords(items[0])
        # Cyan square stays distinct from gold fiducial rings and RGB fills.
        canvas.create_rectangle(left-5, top-5, right+5, bottom+5,
                                outline="black", width=5, tags="selected_site")
        canvas.create_rectangle(left-5, top-5, right+5, bottom+5,
                                outline="#00e5ff", width=3, tags="selected_site")

    def _draw_picklist(self):
        self.design_canvas.delete("selected_site")
        self.picklist_tree.delete(*self.picklist_tree.get_children())
        self.picklist_site_locations = {}
        run = next((run for run in self.runs if run["run_name"] == self.design_name.get()), None)
        self._draw_mixing_recipe(run)
        if run is None:
            self.picklist_summary.set("Prepare runs to see the actual transfers for this design.")
            return
        for transfer in run["picklist"]:
            item = self.picklist_tree.insert("", "end", values=tuple(transfer[column] for column in self.picklist_columns))
            location = transfer.get("site_location")
            if not location:
                match = re.search(r"\((H\d{2}), (\d{2})(?:;|\))", transfer["function"])
                location = match.groups() if match else None
            self.picklist_site_locations[item] = location
        self.picklist_summary.set("{} transfers · {} · wells {}".format(
            len(run["picklist"]), run["destination_plate_name"], ", ".join(run["destination_wells"])))

    def _draw_mixing_recipe(self, run):
        self.recipe_tree.delete(*self.recipe_tree.get_children())
        self.recipe_well_tree.delete(*self.recipe_well_tree.get_children())
        if run is None:
            self.recipe_summary.set("Prepare runs to see the mixing recipe for this design.")
            return
        recipe = run["mixing_recipe"]
        wells = {}
        for transfer in run["picklist"]:
            key = (transfer["Destination Plate Name"], transfer["Destination Well"])
            wells[key] = wells.get(key, 0.0) + float(transfer["Transfer Volume"])
        required = recipe[0]["Volume_uL"]
        collected = sum(wells.values()) / 1000
        final_volume = sum(row["Volume_uL"] for row in recipe)
        self.recipe_summary.set("{} · Staple mix required: {:g} µL · Collected: {:g} µL · "
                                "Final synthesis: {:g} µL. One recipe for this design.".format(
                                    run["run_name"], required, collected, final_volume))
        for index, row in enumerate(recipe):
            label = "Staple mix required (pooled)" if index == 0 else row["Reagent"]
            self.recipe_tree.insert("", "end", values=(label, "{:g}".format(row["Volume_uL"])))
        for (plate, well), volume_nl in wells.items():
            self.recipe_well_tree.insert("", "end", values=(plate, well, "{:g}".format(volume_nl / 1000)))

    def _used_wells(self, plate, ignore_ids=()):
        return occupied_destination_wells(self.app.plate_state, plate, self.app._storage_items(), ignore_ids)


    def _save_template_export_preference(self):
        self.app.config["image_auto_export_templates"] = self.auto_export_templates.get()
        self.app._schedule_config_save()

    def _open_alignment_templates(self):
        if self.alignment_template_directory is not None:
            self.app._open_path(self.alignment_template_directory)

    def _prepare(self):
        if not self.designs:
            messagebox.showinfo("Image required", "Upload an image and enter valid transfer volumes first.", parent=self)
            return
        try:
            fiducial_sites(self.barcode_schema, self._active_columns(), SELECTABLE_SCHEMES[self._scheme_id()].bit_count)
            wells = all_384_wells()
            plate = self.destination_plate.get().strip()
            if not plate or not self.source_plate_type.get().strip() or not self.replacement_plate.get().strip() or not self.biotin_plate.get().strip():
                raise ValueError("Enter RGB destination/source plate names and source plate type.")
            used = self._used_wells(plate)
            available = [well for well in wells if well not in used]
            settings = {**self._transfer_limits(), "destination_plate_name": plate, "source_plate_type": self.source_plate_type.get().strip(),
                        "max_destination_volume_ul": float(self.max_volume.get()),
                        "transfers_per_source": 1, "hinge_type": next(key for key, label in HINGE_TYPES.items() if label == self.hinge_type.get())}
            if not math.isfinite(settings["max_destination_volume_ul"]) or settings["max_destination_volume_ul"] <= 0:
                raise ValueError("RGB maximum well volume must be finite and positive.")
            mixing = self._mixing_settings()
            runs = prepare_runs(self.designs, self.base_path.get(),
                                {color: (view.path_var.get(), view.plate_name) for color, view in self.color_views.items()},
                                available, settings, mixing,
                                source_concentrations_nm={color: view.concentration_nm for color, view in self.color_views.items()},
                                biotin_source=(self.biotin_path.get(), self.biotin_plate.get().strip(), float(self.biotin_stock.get())),
                                biotin_target_nm=float(self.biotin_target.get()))
            batch_id = uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            for run in runs:
                run.update(id=uuid4().hex, created_utc=now, source_run_id=uuid4().hex,
                           image_batch_id=batch_id, source_picklist_path="", source_recipe_path="")
            # Check the decoding experiment before asking the user to select wells.
            guard = getattr(self.app, "barcode_guard", None)
            if guard is not None:
                guard.attach_rgb(runs)
            selected = choose_destination_wells(self, runs, plate, used, self.start_well.get())
            if selected is None:
                return
            runs = assign_destination_wells(runs, selected, self._used_wells(plate))
            if guard is not None:
                # Refresh reservation metadata with the actual selected wells and
                # recheck in case registry state changed while the dialog was open.
                guard.attach_rgb(runs)
            self._invalidate()
            self.batch_id = batch_id
            self.runs = runs
            self._draw_picklist()
            self.snapshot = {"format": "origami-rgb-image-v2", "batch_id": self.batch_id,
                             "color_processing": self._light_options(),
                             "source_image": self.image_path.get(), "fit_mode": self.fit_mode.get(),
                             "origami_rotation_degrees_clockwise": int(self.rotation.get()),
                             "source_image_rotation_degrees": 0,
                             "png_coordinate_frame": "upright image; JSON RGB arrays remain origami-local",
                             "encoding_scheme": self._scheme_id(),
                             "geometry": deepcopy(self.image_geometry), "barcode_schema": deepcopy(self.barcode_schema),
                             "binned_rgb": deepcopy(self.pixels),
                             "quantized_rgb": reconstruct(self.designs),
                             "quantized_rgb_encoding": "linear intensity × 255" if self.linear_light.get() else "legacy direct RGB",
                             "quantized_rgb_role": "per-site intensity reference; predicted appearance is gaussian_reconstruction",
                             "gaussian_reconstruction": deepcopy(self.reconstruction.metadata),
                             "optimization": deepcopy(self.optimization_metrics),
                             "alignment_simulation": deepcopy(self.reconstruction.metadata) if self.alignment_enabled.get() else None,
                             "alignment_example_seed": self.example_seed if self.alignment_enabled.get() else None,
                             "settings": settings, "mixing": mixing,
                             "rgb_run_settings": {key: variable.get() for key, variable in self.run_variables.items()},
                             "designs": deepcopy(self.designs)}
            count = sum(len(run["destination_wells"]) for run in runs)
            self.status.set("Prepared {} independent runs using {} unused wells on {} ({}–{}). Save the batch or send it to storage.".format(
                len(runs), count, plate, runs[0]["destination_wells"][0], runs[-1]["destination_wells"][-1]))
            if self.auto_export_templates.get():
                from alignment_template_export import export_alignment_templates
                root = Path(self.output_root.get()).expanduser()
                if not root.is_absolute():
                    root = self.app_dir / root
                destination = root / "rgb_alignment_templates" / self.batch_id
                self.status.set("Prepared runs. Exporting alignment templates…")
                self.update_idletasks()
                try:
                    manifest = export_alignment_templates(destination, self.snapshot, self.runs)
                except (OSError, ValueError) as exc:
                    self.status.set("Runs prepared, but alignment-template export failed: {}. Saving the image batch will retry the export.".format(exc))
                    messagebox.showerror("Alignment-template export failed", str(exc), parent=self)
                else:
                    self.alignment_template_directory = destination
                    self.open_templates_button.configure(state="normal")
                    self.snapshot["alignment_template_directory"] = str(destination)
                    self.status.set("Prepared {} runs. Exported {} barcode templates to {} ({} all-off designs without bright sites skipped).".format(
                        len(runs), manifest["exported_count"], destination, manifest["skipped_count"]))
            self.export_button.configure(state="normal")
            self.recipe_export_button.configure(state="normal")
            self.store_button.configure(state="normal")
        except Exception as exc:
            self.status.set("Preparation failed: {}".format(exc))
            messagebox.showerror("Cannot prepare image runs", str(exc), parent=self)

    def _check_destinations(self):
        if self.committed:
            return
        for run in self.runs:
            overlap = set(run["destination_wells"]) & self._used_wells(run["destination_plate_name"], {item["id"] for item in self.runs})
            if overlap:
                raise ValueError("Destination wells were used since this preview. Prepare the batch again.")

    def _commit(self):
        if self.committed:
            return
        state = deepcopy(self.app.plate_state)
        record_transfers(state, combine_picklists([run["picklist"] for run in self.runs]))
        save_json(self.plate_path, state)
        self.app.plate_state = state
        self.committed = True
        self.app._refresh_plate_view()

    def _export_similarity_graph(self):
        self._export_exhaustive_pdf(similarity=True)

    def _export_exhaustive_pdf(self, similarity=False, robustness=False):
        parent = self.winfo_toplevel()
        window_name = "similarity_export_window" if similarity else "pdf_export_window"
        button = self.similarity_graph_button if similarity else self.exhaustive_pdf_button
        label = "similarity graph" if similarity else "exhaustive PDF"
        idle_text = "Export similarity graph…" if similarity else "Export exhaustive PDF…"
        if robustness:
            window_name = "robustness_export_window"
            button = self.similarity_optimization_button
            label = "similarity optimization"
            idle_text = "Run similarity optimization"
        try:
            window = getattr(self, window_name, None)
            if window is not None and window.winfo_exists():
                window.deiconify()
                window.lift()
                window.focus_set()
                return
            if self.pixels is None:
                messagebox.showinfo("Image required", "Upload an image before exporting the {}.".format(label), parent=parent)
                return
            # Lazy imports can fail when source files change while the app is open.
            # Report startup errors in the UI instead of losing them in Tk stderr.
            from exhaustive_pdf import ReportSettings, start_export_dialog
            settings = ReportSettings(
                image_path=self.image_path.get(), fit_mode=self.fit_mode.get(),
                base_volume=self._base_transfer_volume(),
                multipliers={color: 1.0 for color in self.color_views},
                **self._concentration_options(),
                fwhm_nm=float(self.gaussian_fwhm.get()), exposure=float(self.gaussian_exposure.get()),
                **self._light_options(), optimize=self.optimize_enabled.get(),
                alignment_enabled=self.alignment_enabled.get(), alignment=self._alignment_settings(),
                example_seed=self.example_seed, regularization_strength=self._regularization_value())
            settings.validate()
            self.status.set("Choose a filename for the {}.".format(label))
            destination = filedialog.asksaveasfilename(parent=parent, title="Run similarity optimization" if robustness else "Export similarity graph" if similarity else "Export exhaustive RGB report",
                defaultextension=".pdf", filetypes=[("PDF report", "*.pdf")],
                initialfile="rgb_similarity_optimization.pdf" if robustness else "rgb_similarity_comparison.pdf" if similarity else "exhaustive_rgb_report.pdf")
            if destination:
                if robustness:
                    from similarity_optimization import export_optimization
                    window = start_export_dialog(self, destination, settings, exporter=export_optimization,
                                                 button=button, graph=True, robustness=True)
                elif similarity:
                    from similarity_graph import export_similarity
                    window = start_export_dialog(self, destination, settings, exporter=export_similarity,
                                                 button=button, graph=True)
                else:
                    window = start_export_dialog(self, destination, settings)
                setattr(self, window_name, window)
                self.status.set("Generating {}. Use the progress button to view or cancel it.".format(label))
            else:
                self.status.set("Export cancelled before choosing a file.")
        except Exception as exc:
            button.configure(state="normal", text=idle_text)
            detail = str(exc) or type(exc).__name__
            if isinstance(exc, ImportError):
                detail += "\nRestart the app to load matching versions of the updated files."
            self.status.set(("Cannot start graph export: " if similarity else "Cannot start PDF export: ") + detail)
            messagebox.showerror("Cannot export " + label, detail, parent=parent)

    def _export_recipes(self):
        if not self.runs:
            return
        folder = filedialog.askdirectory(parent=self, title="Choose parent folder for mixing recipe batch",
                                         initialdir=self.output_root.get())
        if not folder:
            return
        try:
            self._check_destinations()
            output = create_run_output_directory(Path(folder))
            rows = []
            for run in self.runs:
                write_csv(output / run["run_name"] / "mixing_recipe.csv", run["mixing_recipe"],
                          ("Reagent", "Volume_uL"))
                for reagent in run["mixing_recipe"]:
                    rows.append({"Run": run["run_name"], "Destination Plate": run["destination_plate_name"],
                                 "Destination Wells": ", ".join(run["destination_wells"]), **reagent})
            write_csv(output / "mixing_recipe_batch.csv", rows,
                      ("Run", "Destination Plate", "Destination Wells", "Reagent", "Volume_uL"))
            self.status.set("Saved {} separate mixing recipes and mixing_recipe_batch.csv to {}. "
                            "Recipe export does not reserve destination wells.".format(len(self.runs), output))
        except Exception as exc:
            messagebox.showerror("Cannot save mixing recipe batch", str(exc), parent=self)

    def _export(self):
        if not self.runs or self.saved:
            return
        folder = filedialog.askdirectory(parent=self, title="Choose parent folder for image batch",
                                         initialdir=self.output_root.get())
        if not folder:
            return
        try:
            from PIL import Image
            import numpy as np
            self._check_destinations()
            if hasattr(self.app, "barcode_guard"):
                self.app.barcode_guard.reserve_runs(self.runs)
            output = create_run_output_directory(Path(folder))
            from alignment_template_export import export_alignment_templates
            export_alignment_templates(output / "alignment_templates", self.snapshot, self.runs)
            write_csv(output / "picklist_combined.csv", combine_picklists([run["picklist"] for run in self.runs]), PICKLIST_COLUMNS)
            for run in self.runs:
                run_dir = output / run["run_name"]
                write_csv(run_dir / "picklist.csv", run["picklist"], PICKLIST_COLUMNS)
                write_csv(run_dir / "mixing_recipe.csv", run["mixing_recipe"], ("Reagent", "Volume_uL"))
            manifest = deepcopy(self.snapshot)
            manifest["alignment_template_directory"] = "alignment_templates"
            manifest["runs"] = [{key: run[key] for key in ("run_name", "destination_plate_name", "destination_wells", "transfer_count", "d_biotin", "id", "source_run_id", "barcode_experiment")} for run in self.runs]
            save_json(output / "image_manifest.json", manifest)
            pixel_rows = []
            for design in self.snapshot["designs"]:
                for site in design["image_sites"]:
                    pixel_rows.append({"design": design["name"], "color": design["color"],
                                       "encoding_scheme": design["encoding_scheme"], "barcode_code": design["barcode_code"],
                                       "barcode_bits": "".join(map(str, design["bits"])),
                                       "x_shift": design["x_shift"], "y_shift": design["y_shift"],
                                       **{key: design[key] for key in ("quadrant", "quadrant_x", "quadrant_y", "local_x_shift", "local_y_shift", "local_x_states", "local_y_states", "quadrant_normalization_weight", "quadrant_offset_x_nm", "quadrant_offset_y_nm") if key in design},
                                       **{key: value for key, value in site.items() if key != "rgb"},
                                       "R": site["rgb"][0], "G": site["rgb"][1], "B": site["rgb"][2]})
            write_csv(output / "pixel_assignments.csv", pixel_rows, list(pixel_rows[0]))
            for key in ("binned_rgb", "quantized_rgb"):
                raster = Image.new("RGB", (self.snapshot["geometry"]["columns"], self.snapshot["geometry"]["rows"]))
                raster.putdata([tuple(pixel) for row in self.snapshot[key] for pixel in row])
                if key == "quantized_rgb" and self.snapshot.get("color_processing", {}).get("linear_light"):
                    from color_light import display_rgb
                    raster = display_rgb(np.asarray(raster, dtype=float)/255, True)
                rotation = self.snapshot["origami_rotation_degrees_clockwise"]
                physical = physical_reference_raster(raster,self.snapshot["encoding_scheme"],rotation, self.snapshot["geometry"].get("active_image_columns"))
                raster = raster.rotate(-rotation, expand=True)
                raster.save(output / (key + ".png"))
                physical.save(output / (key + "_physical.png"))
            overlay_settings = self.snapshot["gaussian_reconstruction"]
            light_options = {key: overlay_settings.get(key, False) for key in ("linear_light", "normalize_designs", "compensate_area")}
            if self.snapshot.get("alignment_simulation"):
                alignment = self.snapshot["alignment_simulation"]
                model = AlignmentModel(self.snapshot["designs"], AlignmentSettings(**alignment["alignment"]),
                                       overlay_settings["fwhm_nm"], overlay_settings["exposure"], seed=alignment["model_seed"], **light_options)
                overlay = model.expected(self.snapshot["designs"])
                example = model.example(self.snapshot["designs"], self.snapshot["alignment_example_seed"])
                example.rgb.save(output / "alignment_example.png")
                np.savez_compressed(output / "alignment_example_density.npz", density=example.density,
                                    pixel_x_nm=example.metadata["raster_pixel_x_nm"], pixel_y_nm=example.metadata["raster_pixel_y_nm"])
                render_gaussian_overlay(self.snapshot["designs"], model.fwhm, model.exposure, **light_options).rgb.save(output / "ideal_alignment.png")
            else:
                overlay = render_gaussian_overlay(self.snapshot["designs"], overlay_settings["fwhm_nm"], overlay_settings["exposure"], **light_options)
            overlay.rgb.save(output / "gaussian_reconstruction.png")
            np.savez_compressed(output / "gaussian_reconstruction_density.npz",
                                density=overlay.density, pixel_x_nm=overlay.metadata["raster_pixel_x_nm"],
                                pixel_y_nm=overlay.metadata["raster_pixel_y_nm"])
            self._commit()
            self.saved = True
            self.export_button.configure(state="disabled")
            self.status.set("Saved all {} picklists, recipes, pixel assignments, previews, and manifest to {}".format(len(self.runs), output))
        except Exception as exc:
            messagebox.showerror("Cannot save image batch", str(exc), parent=self)

    def _store(self):
        if not self.runs or self.stored:
            return
        try:
            self._check_destinations()
            if hasattr(self.app, "barcode_guard"):
                self.app.barcode_guard.reserve_runs(self.runs)
            state = deepcopy(self.app.storage_state)
            # Keep the complete image mapping with each stored design so it survives restarts.
            existing_ids = {item.get("id") for item in state.get("items", [])}
            for run in self.runs:
                if run["id"] in existing_ids:
                    continue
                item = deepcopy(run)
                item["gaussian_reconstruction"] = deepcopy(self.snapshot["gaussian_reconstruction"])
                item["alignment_simulation"] = deepcopy(self.snapshot.get("alignment_simulation"))
                item["alignment_example_seed"] = self.snapshot.get("alignment_example_seed")
                item["color_processing"] = deepcopy(self.snapshot.get("color_processing"))
                item["image_optimization"] = deepcopy(self.snapshot.get("optimization"))
                item["image_geometry"] = deepcopy(self.snapshot["geometry"])
                item["image_encoding_scheme"] = self.snapshot["encoding_scheme"]
                item["image_source"] = self.snapshot["source_image"]
                item["origami_rotation_degrees_clockwise"] = self.snapshot["origami_rotation_degrees_clockwise"]
                item["source_image_rotation_degrees"] = 0
                item["image_fit_mode"] = self.snapshot["fit_mode"]
                state.setdefault("items", []).append(item)
            state["version"] = 2
            state["updated_utc"] = datetime.now(timezone.utc).isoformat()
            save_json(self.storage_path, state)
            self.app.storage_state = state
            self._commit()
            self.stored = True
            self.store_button.configure(state="disabled")
            self.app._refresh_storage_tree()
            self.status.set("Stored all {} image designs with separate destination wells and mixing recipes.".format(len(self.runs)))
        except Exception as exc:
            messagebox.showerror("Cannot store image batch", str(exc), parent=self)


class EncodingComparison(tk.Toplevel):
    """Compare all schemes at the same physical scale without preparing any runs."""

    def __init__(self, owner):
        super().__init__(owner.winfo_toplevel())
        self.owner = owner
        self.title("Compare original RGB encoding schemes")
        self.geometry("1200x700")
        self.minsize(620, 400)
        self.summary = tk.StringVar()
        ttk.Label(self, text="Compare encoding schemes", font=("TkDefaultFont", 16, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(self, text="Previews fit each complete field to the same display width; physical field sizes are listed. Quadrant versions appear below their base schemes; scroll down to compare. They use a 118.8 × 81.6 nm virtual field. Sampling pitch is not resolution. Barcode errors are not simulated; fiducial sites must be reserved separately.",
                  wraplength=1100).pack(anchor="w", padx=12, pady=(0, 8))
        loading = ttk.Frame(self)
        loading.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(loading, text="Compare tiling / local-shift sweeps…", command=owner._compare_tiling).pack(anchor="w")
        self.progress_text = tk.StringVar(value="Ready to compare")
        ttk.Label(loading, textvariable=self.progress_text).pack(anchor="w")
        self.progress = ttk.Progressbar(loading, mode="determinate", maximum=len(ENCODING_SCHEMES))
        self.progress.pack(fill="x", pady=(4, 0))
        host = ttk.Frame(self)
        host.pack(fill="both", expand=True, padx=8)
        viewport = tk.Canvas(host, highlightthickness=0)
        horizontal = ttk.Scrollbar(host, orient="horizontal", command=viewport.xview)
        vertical = ttk.Scrollbar(host, orient="vertical", command=viewport.yview)
        viewport.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        viewport.grid(row=0, column=0, sticky="nsew")
        horizontal.grid(row=1, column=0, sticky="ew")
        vertical.grid(row=0, column=1, sticky="ns")
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)
        cards = ttk.Frame(viewport)
        viewport.create_window(0, 0, window=cards, anchor="nw")
        cards.bind("<Configure>", lambda _event: viewport.configure(scrollregion=viewport.bbox("all")))
        self.metrics = {}
        self.previews = {}
        self.buttons = {}
        self.images = {}
        self.optimization_metrics = {}
        self._binned_cache = {}
        self._source_key = None
        family_columns = {key: index for index, key in enumerate(
            key for key, scheme in ENCODING_SCHEMES.items() if not scheme.tiled)}
        for index, (scheme_id, scheme) in enumerate(ENCODING_SCHEMES.items()):
            info = geometry(scheme_id)
            card = ttk.LabelFrame(cards, text="{}-bit {}×{}{}".format(scheme.bit_count, scheme.x_states, scheme.y_states, " · 4 quadrants" if scheme.tiled else (" + parity" if scheme.parity else "")), padding=6)
            card.grid(row=1 if scheme.tiled else 0,
                      column=family_columns[scheme.parent_scheme if scheme.tiled else scheme_id],
                      sticky="n", padx=4, pady=4)
            self.metrics[scheme_id] = tk.StringVar()
            ttk.Label(card, justify="center", textvariable=self.metrics[scheme_id]).pack(pady=(0, 6))
            canvases = []
            for label in ("Binned RGB", "Predicted Gaussian overlay"):
                ttk.Label(card, text=label).pack(anchor="w", pady=(4, 2))
                canvas = tk.Canvas(card, width=264, height=187, background="#15191d", highlightthickness=0)
                canvas.pack()
                canvases.append(canvas)
            button = ttk.Button(card, text="Use this scheme", command=lambda key=scheme_id: self.use_scheme(key))
            button.pack(pady=(10, 4))
            self.previews[scheme_id] = canvases
            self.buttons[scheme_id] = button
        ttk.Label(self, textvariable=self.summary, wraplength=1100).pack(anchor="w", padx=12, pady=10)

    def use_scheme(self, scheme_id):
        if self.owner._scheme_id() != scheme_id:
            self.owner.scheme.set(ENCODING_SCHEMES[scheme_id].label)

    def _show_progress(self, completed, message):
        self.progress.configure(value=completed)
        self.progress_text.set(message)
        # Paint the window and bar before entering each expensive computation.
        # Avoid update(), which could re-enter refresh via queued user actions.
        self.update_idletasks()

    def refresh(self):
        owner = self.owner
        total = len(ENCODING_SCHEMES)
        self.configure(cursor="watch")
        for button in self.buttons.values():
            button.configure(state="disabled")
        self._show_progress(0, "Preparing comparison…")
        try:
            if owner.pixels is None:
                raise ValueError("Upload a valid image to compare the schemes.")
            path = Path(owner.image_path.get())
            source_key = (str(path), path.stat().st_mtime_ns, path.stat().st_size,
                          owner.fit_mode.get(), int(owner.rotation.get()), owner.linear_light.get(), tuple(owner._active_columns()))
            if source_key != self._source_key:
                self._binned_cache = {}
                self._source_key = source_key
            images = {}
            optimization_metrics = {}
            for index, (scheme_id, scheme) in enumerate(ENCODING_SCHEMES.items()):
                label = "{}-bit {}×{}".format(scheme.bit_count, scheme.x_states, scheme.y_states)
                self._show_progress(index, "{} of {} complete · Loading {}…".format(index, total, label))
                if scheme_id not in self._binned_cache:
                    self._binned_cache[scheme_id] = bin_image(path, owner.fit_mode.get(), int(owner.rotation.get()), scheme_id, linear_light=owner.linear_light.get(), active_image_columns=owner._active_columns())
                binned = self._binned_cache[scheme_id]
                designs = owner._compile_scheme(binned, scheme_id)
                if owner.optimize_enabled.get():
                    self._show_progress(index, "{} of {} complete · Optimizing {}…".format(index, total, label))
                    designs, optimization_metrics[scheme_id] = owner._optimize(designs)
                self._show_progress(index, "{} of {} complete · Reconstructing {}…".format(index, total, label))
                achievable = owner._render_overlay(designs)
                images[scheme_id] = (binned, achievable)
                self._show_progress(index + 1, "{} of {} computed".format(index + 1, total))
            self._show_progress(total, "Drawing comparison previews…")
            self.images = images
            self.optimization_metrics = optimization_metrics
            for scheme_id, (binned, achievable) in images.items():
                info = geometry(scheme_id, int(owner.rotation.get()), owner._active_columns())
                self.metrics[scheme_id].set(("{display_columns} × {display_rows} pixels · {design_count} designs\n"
                    "{display_pixel_pitch_x_nm:.4g} × {display_pixel_pitch_y_nm:.4g} nm/pixel").format(**info))
                if scheme_id in optimization_metrics:
                    metrics = optimization_metrics[scheme_id]
                    self.metrics[scheme_id].set(self.metrics[scheme_id].get() + "\nOptimized RMSE {:.4f} → {:.4f}\nSquared error reduced {:.1%}".format(
                        metrics["baseline_rmse"], metrics["optimized_rmse"], metrics["mse_reduction_fraction"]))
                if scheme_id in optimization_metrics and "validation_optimized" in optimization_metrics[scheme_id]:
                    metrics = optimization_metrics[scheme_id]
                    self.metrics[scheme_id].set(self.metrics[scheme_id].get() + "\nHeld-out RMSE {:.4f} → {:.4f}".format(
                        metrics["validation_baseline"]["expected_mse"]**.5, metrics["validation_optimized"]["expected_mse"]**.5))
                scheme = ENCODING_SCHEMES[scheme_id]
                detail = "\n{} bits · {} sites/bit · {}".format(scheme.bit_count, info["sites_per_bit"],
                    "even parity" if scheme.parity else "no parity")
                detail += "\n{display_field_width_nm:g} × {display_field_height_nm:g} nm field".format(**info)
                if scheme.tiled:
                    detail += "\nQuadrant shifts: " + " / ".join("{}×{}".format(q["x_states"],q["y_states"]) for q in info["quadrant_sampling"])
                    if info["nonuniform_sampling"]:
                        detail += "\nUnequal sampling; equal physical areas"
                detail += "\nCanvas exposure compensation {:g}×".format(achievable.metadata.get("canvas_area_gain", 1))
                detail += "\nAt least {} destination wells".format(scheme.design_count)
                if scheme.design_count > 384:
                    detail += " (exceeds one plate)"
                if owner.alignment_enabled.get():
                    detail += "\n{} origami total at {} / design".format(
                        scheme.design_count*owner._alignment_settings().origami_per_design,
                        owner._alignment_settings().origami_per_design)
                self.metrics[scheme_id].set(self.metrics[scheme_id].get()+detail)
                owner._draw_pixels(self.previews[scheme_id][0], binned, info, draw_width=252)
                owner._draw_overlay(self.previews[scheme_id][1], achievable, info, draw_width=252)
                active = scheme_id == owner._scheme_id()
                self.buttons[scheme_id].configure(text="Selected" if active else "Use this scheme", state="disabled" if active else "normal")
            settings_summary = ("Replacement optimization ON — applied independently to every scheme. "
                                if owner.optimize_enabled.get() else "Replacement optimization OFF for every scheme. ")
            settings_summary += "Regularization {} (0 = off). ".format(owner.regularization.get())
            settings_summary += "Base transfer {} nL; plate targets {}. ".format(
                owner.transfer_volume.get(), ", ".join(
                    "{} {} nM".format(color, view.desired_concentration_nm.get()) for color, view in owner.color_views.items()))
            if owner.alignment_enabled.get():
                alignment = owner._alignment_settings()
                settings_summary += ("Alignment: X SD {translation_x_nm:g} nm, Y SD {translation_y_nm:g} nm, "
                    "rotation SD {rotation_deg:g}°, {origami_per_design} origami/design, "
                    "center ({center_x_nm:g}, {center_y_nm:g}) nm, variability penalty {variability_penalty:g}. "
                    "Previews show expected alignment results. ").format(**vars(alignment))
            else:
                settings_summary += "Alignment uncertainty OFF. "
            light_summary = ("Linear-light processing. " if owner.linear_light.get() else "Legacy direct RGB processing. ")
            light_summary += ("Fixed total signal across schemes (48/design-count weighting); unequal quadrant sampling is balanced by per-design weights; no automatic mixing changes. "
                              if owner.normalize_designs.get() else "Equal per-design weight: more designs produce more total signal. ")
            light_summary += "Canvas-area exposure compensation enabled. " if owner.compensate_area.get() else "No canvas-area exposure compensation. "
            self.summary.set(settings_summary + light_summary + "{} · origami {}° clockwise · FWHM {} nm · shared exposure {}×. Equal channel brightness. Clipped pixels: {}.".format(
                owner.fit_mode.get(), owner.rotation.get(), owner.gaussian_fwhm.get(), owner.gaussian_exposure.get(),
                "; ".join("{}×{}: {:.1%}".format(ENCODING_SCHEMES[key].x_states, ENCODING_SCHEMES[key].y_states,
                         result.metadata["clipped_pixel_fraction"]) for key, (_, result) in images.items())))
            self._show_progress(total, "Comparison complete · {} of {} schemes".format(total, total))
        except Exception as exc:
            self._show_progress(0, "Comparison could not be completed")
            self.images = {}
            self.optimization_metrics = {}
            for metric in self.metrics.values():
                metric.set("")
            for canvases in self.previews.values():
                for canvas in canvases:
                    canvas.delete("all")
            for button in self.buttons.values():
                button.configure(state="disabled")
            self.summary.set("Cannot compare: {}".format(exc))
        finally:
            self.configure(cursor="")
