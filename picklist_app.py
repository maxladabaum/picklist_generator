"""Desktop GUI for creating Echo picklists and mixing recipes."""

from __future__ import annotations

import os
import json
import math
import re
import subprocess
import sys
import tkinter as tk
from copy import deepcopy
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from app_state import (
    PLATE_ROWS,
    all_384_wells,
    clear_plate,
    create_run_output_directory,
    edit_destination_well,
    load_json,
    next_unused_wells,
    normalize_recent_wells,
    plate_wells,
    record_transfers,
    save_json,
    unused_wells_from,
)
from picklist_core import (
    HINGE_TYPES,
    PICKLIST_COLUMNS,
    ReplacementSelection,
    calculate_mixing_volumes,
    combine_picklists,
    generate_picklist,
    parse_vector,
    parse_source2,
    replacement_names,
    replacement_fractions,
    desired_concentration_ratio,
    separate_mixing_recipes,
    valid_well_list,
    write_csv,
)
from image_encoding import BIT_SITES, COLOR_BITS, SHIFT_STATES, synthesis_base_volume_nl
from rgb_image_view import RGBImageView
from mirna_replacements import target_designs, anchor_position, replacement_positions
from selection_report import write_replacement_selection_pdf, write_stored_runs_selection_pdf
from template_generator import (
    EXTENSION_COLUMN_SPACING_NM,
    EXTENSION_EXTRA_COLUMN_GAP_NM,
    EXTENSION_ROW_SPACING_NM,
    empty_logical_bit_schema,
    logical_group_color,
    logical_template_sites,
    normalize_logical_bit_schema,
    save_barcode_template,
    template_size_nm,
    template_column_positions,
    validate_template_settings,
)


APP_VERSION = "2.0.0"
APP_TITLE = "Echo Picklist Generator v{}".format(APP_VERSION)
APP_DIR = Path(__file__).resolve().parent
SHEET_DIR = APP_DIR / "replacement_sheets"
OUTPUT_DIR = APP_DIR / "generated_output"
CONFIG_PATH = APP_DIR / "config.json"
PLATE_STATE_PATH = APP_DIR / "destination_plate_state.json"
STORAGE_STATE_PATH = APP_DIR / "storage_state.json"
CONFIG_EXAMPLE_PATH = APP_DIR / "config.example.json"
PLATE_STATE_EXAMPLE_PATH = APP_DIR / "destination_plate_state.example.json"
STORAGE_STATE_EXAMPLE_PATH = APP_DIR / "storage_state.example.json"

DEFAULT_CONFIG = {
    "version": 1,
    "hinge_type": "original",
    "save_picklist_path": "generated_output/picklist_combined.csv",
    "save_recipe_path": "generated_output/mixing_recipe.csv",
    "output_root_path": "generated_output",
    "pending_destination_wells": ["A01", "A02", "A03"],
    "recent_destination_wells": [],
    "destination_plate_name": "Destination[1]",
    "source_plate_type": "384PP_AQ_BP",
    "transfers_per_source": 1,
    "transfer_volume_nL": 25,
    "max_destination_volume_uL": 12.5,
}
BASE_PLATES = {
    "Base plate — 200 µM": ("book_base.csv", 200000.0),
    "LowConc base plate — 20 µM (original layout)": ("book_base_20uM.csv", 20000.0),
    "LowConc base plate — 20 µM (interleaved)": ("book_base_20uM_interleaved.csv", 20000.0),
}

DEFAULT_PLATE_STATE = {"version": 1, "plates": {}}
DEFAULT_STORAGE_STATE = {"version": 2, "items": []}

ROWS = ["H01", "H03", "H05", "H07", "H09", "H11", "H13", "H15"]
COLS = ["{:02d}".format(i) for i in range(1, 13)]
R1_SELECTION_COLORS = {"R1 R": "#ff0000", "R1 B": "#0000ff", "R1 G": "#00ff00"}
PAINT_REPLACEMENT_TABS = {"PAINT P1", "PAINT R1", "LowConc PAINT R1"}
ACTIVE_REPLACEMENT_TABS = PAINT_REPLACEMENT_TABS | {
    "Yaritza Extensions", "Aptamers", "Soyeon MB", "miRNA", "MB",
}


class ScrollFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")


class SetView:
    SELECTABLE_BG = "#ffffff"
    SELECTABLE_HOVER_BG = "#e7f5ec"
    SELECTED_BG = "#35a85b"
    CONFLICT_BG = "#cc3333"
    UNAVAILABLE_BG = "#c8ccd0"

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        csv_path: Path,
        plate_name: str,
        panels: Sequence[Dict[str, object]],
        selection_changed=None,
        template_requested=None,
        base_transfer_volume=None,
        scrollable=True,
    ) -> None:
        self.title = title
        self.selection_color = R1_SELECTION_COLORS.get(title, "#00ffff")
        self.encoding_enabled = tk.BooleanVar(value=False)
        self.x_shift = tk.StringVar(value=next(iter(SHIFT_STATES)))
        self.y_shift = tk.StringVar(value=next(iter(SHIFT_STATES)))
        self.encoded_sites = {}
        if title in COLOR_BITS:
            for bit, sites in enumerate(BIT_SITES):
                for row, column in sites:
                    self.encoded_sites[panel_sequence_name(panels[0], row, column)] = bit
        self.path_var = tk.StringVar(value=str(csv_path))
        self.concentration_nm = 20000.0 if title == "LowConc PAINT R1" or title in R1_SELECTION_COLORS else 200000.0
        self.base_concentration_nm = 200000.0
        self.desired_concentration_nm = tk.StringVar(value="25" if title == "LowConc PAINT R1" or title in R1_SELECTION_COLORS else ("250" if title in PAINT_REPLACEMENT_TABS else "50"))
        self.base_desired_concentration_nm = 50.0
        self.replacement_percentage = tk.StringVar(value="100%")
        self.site_fractions: Dict[str, float] = {}
        self.smart_absorbed = set()
        self.site_concentrations_nm: Dict[str, float] = {}
        self.new_selection_concentration = tk.StringVar(value="")
        self.minimum_transfer_nl = 25.0
        self.transfer_increment_nl = 25.0
        self.transfer_volume_nl = 25
        self.replacement_error = tk.StringVar()
        self.excess_multiplier = tk.StringVar(value="5" if title in PAINT_REPLACEMENT_TABS else "1")
        self.base_transfer_volume = base_transfer_volume if base_transfer_volume is not None else tk.StringVar(value="25")
        self.plate_name = plate_name
        self.variables: Dict[str, tk.BooleanVar] = {}
        self.buttons: Dict[str, tk.Checkbutton] = {}
        self.circle_sites = {}
        self.circle_radius_nm = 2.5
        self.circle_radius_var = tk.StringVar(value="2.5")
        self.circle_radius_error = tk.StringVar()
        self.color_frames: Dict[str, tk.Frame] = {}
        self.selectable: Dict[str, bool] = {}
        self.available: Set[str] = set()
        self.conflicts: Set[str] = set()
        self.selection_changed = selection_changed
        self.template_requested = template_requested
        self.panels = panels
        self.r1_locations = {name: site for panel in panels for name, site in panel.get("r1_locations", {}).items()}
        self._panel_cache = {}

        if scrollable:
            scroll = ScrollFrame(parent)
            scroll.pack(fill="both", expand=True)
            content = scroll.inner
        else:
            content = ttk.Frame(parent)
            content.pack(fill="both", expand=True)
        toolbar = ttk.Frame(content)
        toolbar.pack(fill="x", padx=10, pady=(3, 1))
        ttk.Label(toolbar, text="{} replacement sheet".format(title)).pack(side="left")
        ttk.Label(toolbar, text="  Stock: {:g} µM".format(self.concentration_nm / 1000)).pack(side="left")
        self.sheet_path_entry = ttk.Entry(toolbar, textvariable=self.path_var)
        self.sheet_path_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.sheet_browse_button = ttk.Button(toolbar, text="Browse…", command=self._browse)
        self.sheet_browse_button.pack(side="left")
        self.biotin_sheet_path = tk.StringVar(value=str(SHEET_DIR / "lowconc_D_biotin_additions.csv"))
        self.source_plate_label = tk.StringVar(value=plate_name)
        ttk.Label(toolbar, textvariable=self.source_plate_label).pack(side="left", padx=5)
        ttk.Button(toolbar, text="Clear set", command=self.clear).pack(side="left", padx=(8, 0))
        if title == "LowConc PAINT R1":
            ttk.Label(content, text="D-Biotin: LowConc base plate SourcePlate8[8], wells P17–P24, stock 20 µM. "
                      "Uses the plate default or a per-site concentration override.").pack(anchor="w", padx=12)
        volume_bar = ttk.Frame(content)
        volume_bar.pack(fill="x", padx=12, pady=1)
        ttk.Label(volume_bar, text="Calculated base transfer (nL/staple, all manual tabs)").pack(side="left")
        ttk.Entry(volume_bar, textvariable=self.base_transfer_volume, state="readonly", width=8).pack(side="left", padx=8)
        concentration_bar = ttk.Frame(volume_bar)
        concentration_bar.pack(side="left", padx=(12, 0))
        ttk.Label(concentration_bar, text="Plate default (nM)").pack(side="left")
        ttk.Entry(concentration_bar, textvariable=self.desired_concentration_nm, width=8).pack(side="left", padx=8)
        ttk.Label(concentration_bar, text="New selections (nM; blank = default)").pack(side="left")
        ttk.Entry(concentration_bar, textvariable=self.new_selection_concentration, width=8).pack(side="left", padx=8)
        ttk.Label(concentration_bar, text="Replacement % for new selections").pack(side="left", padx=(12, 0))
        self.percentage_picker = ttk.Combobox(
            concentration_bar, textvariable=self.replacement_percentage, width=12, state="readonly")
        self.percentage_picker.pack(side="left", padx=8)
        self.available_levels = tk.StringVar()
        replacement_error_label = ttk.Label(content, textvariable=self.replacement_error,
                                            foreground="#a40000", wraplength=1000)
        def show_replacement_error(*_args):
            if self.replacement_error.get():
                replacement_error_label.pack(anchor="w", padx=12, after=volume_bar)
            else:
                replacement_error_label.pack_forget()
        self.replacement_error.trace_add("write", show_replacement_error)
        show_replacement_error()
        if self.title in COLOR_BITS:
            encoding_bar = ttk.Frame(content)
            encoding_bar.pack(fill="x", padx=12, pady=4)
            ttk.Checkbutton(encoding_bar, text="Include this color in run", variable=self.encoding_enabled,
                            command=self._encoding_changed).pack(side="left", padx=(0, 12))
            for label, variable in (("X-shift (bits 3, 4)", self.x_shift), ("Y-shift (bits 5, 6)", self.y_shift)):
                ttk.Label(encoding_bar, text=label).pack(side="left")
                picker = ttk.Combobox(encoding_bar, textvariable=variable, values=list(SHIFT_STATES),
                                      state="readonly", width=19)
                picker.pack(side="left", padx=8)
                picker.bind("<<ComboboxSelected>>", self._shift_changed)
            ttk.Label(content, text="Bits 1–2 identify the color; bits 3–6 follow the shift dropdowns. "
                      "Color bits stay displayed when excluded from the run; encoded sites are locked.").pack(anchor="w", padx=12)
        panel_bar = ttk.Frame(content)
        panel_bar.pack(fill="x", padx=12, pady=(2, 1))
        ttk.Label(panel_bar, text="Left (01–06):" if title == "Aptamers" else "Panel:").pack(side="left")
        self.panel_var = tk.StringVar(value=str(self.panels[0]["label"]))
        self.panel_picker = ttk.Combobox(
            panel_bar,
            textvariable=self.panel_var,
            values=[str(panel["label"]) for panel in self.panels
                    if title != "Aptamers" or panel["label"] in {"PDGF-Apt", "Kana-Apt"}],
            state="readonly",
            width=24,
        )
        self.panel_picker.pack(side="left", padx=(6, 12))
        self.panel_picker.bind("<<ComboboxSelected>>", lambda _event: self._show_panel())
        if title == "Aptamers":
            right_labels = [str(panel["label"]) for panel in self.panels
                            if panel["label"] not in {"PDGF-Apt", "Kana-Apt"}]
            self.right_panel_var = tk.StringVar(value=right_labels[0])
            ttk.Label(panel_bar, text="Right (07–12):").pack(side="left")
            self.right_panel_picker = ttk.Combobox(
                panel_bar, textvariable=self.right_panel_var, values=right_labels,
                state="readonly", width=18,
            )
            self.right_panel_picker.pack(side="left", padx=(6, 12))
            self.right_panel_picker.bind("<<ComboboxSelected>>", lambda _event: self._show_panel())
        if self.template_requested is not None:
            ttk.Button(
                panel_bar,
                text="Left template" if title == "Aptamers" else "Make template from this panel",
                command=self._request_template,
            ).pack(side="right")
            if title == "Aptamers":
                ttk.Button(panel_bar, text="Right template", command=lambda: self._request_template(
                    next(panel for panel in self.panels if panel["label"] == self.right_panel_var.get())
                )).pack(side="right")
        self.selection_summary = tk.StringVar(value="0 selected in this set")
        summary_bar = panel_bar
        if title == "Aptamers":
            summary_bar = ttk.Frame(content)
            summary_bar.pack(fill="x", padx=12)
        ttk.Label(summary_bar, textvariable=self.selection_summary, foreground="#555555").pack(side="left")
        self.orientation_summary = tk.StringVar()
        ttk.Label(summary_bar, textvariable=self.orientation_summary, foreground="#3d626f").pack(
            side="left", padx=(12, 0)
        )
        display_controls = ttk.Frame(content)
        display_controls.pack(fill="x", padx=12, pady=1)
        if self.title in {"Aptamers", "MB"} | PAINT_REPLACEMENT_TABS:
            legend = tk.Frame(display_controls, background="#f0f0f0")
            legend.pack(side="left")
            self._legend_square(legend, self.SELECTABLE_BG, "").pack(side="left")
            tk.Label(legend, text=" Selectable", background="#f0f0f0").pack(side="left", padx=(0, 14))
            self._legend_square(legend, self.selection_color if self.title == "Aptamers" or any(panel.get("spacing_x_nm") is not None for panel in self.panels) else self.SELECTED_BG, "✓", foreground="#222222").pack(side="left")
            tk.Label(legend, text=" Selected", background="#f0f0f0").pack(side="left", padx=(0, 14))
            self._legend_square(legend, self.UNAVAILABLE_BG, "×", foreground="#62676b").pack(side="left")
            tk.Label(legend, text=" Not selectable", background="#f0f0f0").pack(side="left")
        self.panel_area = ttk.Frame(content)
        self.panel_area.pack(fill="both", expand=True, padx=10, pady=1)
        self.panel_host = ttk.Frame(self.panel_area)
        self.panel_host.pack(side="left", anchor="n")
        if self.title == "Aptamers" or any(panel.get("spacing_x_nm") is not None for panel in self.panels):
            radius_bar = ttk.Frame(self.panel_area)
            radius_bar.pack(side="left", anchor="n", padx=(12, 0), pady=(24, 0))
            ttk.Label(radius_bar, text="Circle radius (nm)").pack(anchor="w")
            ttk.Spinbox(radius_bar, from_=0.1, to=50.0, increment=0.5,
                        textvariable=self.circle_radius_var, width=8).pack(anchor="w", pady=(3, 0))
            ttk.Label(radius_bar, textvariable=self.circle_radius_error,
                      foreground="#a40000", wraplength=160).pack(anchor="w")
        self.refresh()
        self.circle_radius_var.trace_add("write", self._circle_radius_changed)
        self.desired_concentration_nm.trace_add("write", self._concentration_changed)
        self.new_selection_concentration.trace_add("write", lambda *_args: self.update_percentage_options(self.transfer_volume_nl))
        self.base_transfer_volume.trace_add("write", lambda *_args: self.update_percentage_options(self.base_transfer_volume.get()))
        self.update_percentage_options(self.base_transfer_volume.get())

    def _apply_encoding(self) -> None:
        if self.title not in COLOR_BITS:
            return
        states = COLOR_BITS[self.title] + SHIFT_STATES[self.x_shift.get()] + SHIFT_STATES[self.y_shift.get()]
        for name, bit in self.encoded_sites.items():
            self.site_fractions.pop(name, None)
            self.site_concentrations_nm.pop(name, None)
            if name in self.variables:
                self.variables[name].set((bit < 2 or self.encoding_enabled.get())
                                         and states[bit] and self.selectable.get(name, False))
                self._paint_square(name)

    def _encoding_changed(self) -> None:
        self._apply_encoding()
        self.mark_conflicts(set())
        self._update_selection_summary()
        if self.selection_changed is not None:
            self.selection_changed()

    def _shift_changed(self, _event=None) -> None:
        self.encoding_enabled.set(True)
        self._encoding_changed()

    def update_percentage_options(self, volume) -> None:
        self.transfer_volume_nl = volume
        try:
            fractions = replacement_fractions(
                float(volume), float(self.new_selection_concentration.get().strip() or self.desired_concentration_nm.get()),
                self.base_desired_concentration_nm,
                self.base_concentration_nm / self.concentration_nm,
                self.minimum_transfer_nl, self.transfer_increment_nl,
            )
            self.replacement_error.set("")
        except ValueError as exc:
            fractions = []
            self.replacement_error.set("No available replacement levels: {}".format(exc))
        self._fraction_options = {"{:.10g}%".format(f * 100): f for f in fractions}
        self.percentage_picker.configure(values=list(self._fraction_options))
        if self.replacement_percentage.get() not in self._fraction_options:
            self.replacement_percentage.set(next(reversed(self._fraction_options), ""))
        displayed_levels = list(self._fraction_options)
        if len(displayed_levels) > 16:
            displayed_levels = displayed_levels[:5] + ["…"] + displayed_levels[-5:]
        self.available_levels.set("Available replacement levels: {} ({} levels). Minimum {:g} nL; increment {:g} nL.".format(
            ", ".join(displayed_levels) if displayed_levels else "none", len(self._fraction_options), self.minimum_transfer_nl, self.transfer_increment_nl))
        for name, fraction in self.site_fractions.items():
            try:
                options = replacement_fractions(float(volume), self.site_target(name),
                    self.base_desired_concentration_nm, self.base_concentration_nm / self.concentration_nm,
                    self.minimum_transfer_nl, self.transfer_increment_nl)
                if not any(math.isclose(fraction, option, rel_tol=0, abs_tol=1e-9) for option in options):
                    raise ValueError("Choose one of {}.".format(", ".join("{:g}%".format(100*f) for f in options)))
            except ValueError as exc:
                self.replacement_error.set("{}: selected target/percentage is not feasible ({:g}%). {} Reselect this site or adjust synthesis settings.".format(name,100*fraction,exc))
                break

    def site_target(self, name: str) -> float:
        if name in self.site_concentrations_nm:
            return self.site_concentrations_nm[name]
        return float(self.desired_concentration_nm.get())

    def _target_label(self, name: str) -> str:
        if name in self.smart_absorbed:
            return "via lock"
        try:
            target = self.site_target(name)
            return "{:g} nM".format(target) if math.isfinite(target) and target > 0 else "Invalid nM"
        except ValueError:
            return "Invalid nM"

    def _concentration_changed(self, *_args):
        self.update_percentage_options(self.transfer_volume_nl)
        for name in self.buttons.keys() | self.circle_sites.keys():
            self._paint_square(name)

    def _selected_highlight(self, name: str, background: str, highlight: str) -> str:
        alpha = self.site_fractions.get(name, 1.0)
        base = [int(background[i:i + 2], 16) for i in (1, 3, 5)]
        color = [int(highlight[i:i + 2], 16) for i in (1, 3, 5)]
        return "#{:02x}{:02x}{:02x}".format(*(round(b * (1 - alpha) + c * alpha) for b, c in zip(base, color)))

    def _circle_radius_changed(self, *_args):
        try:
            radius = float(self.circle_radius_var.get())
            if not math.isfinite(radius) or radius <= 0 or radius > 50:
                raise ValueError
        except ValueError:
            self.circle_radius_error.set("Enter a radius greater than 0 and at most 50 nm.")
            return
        self.circle_radius_error.set("")
        if radius == self.circle_radius_nm:
            return
        self.circle_radius_nm = radius
        self._clear_panel_cache()
        self._show_panel()

    def _browse(self) -> None:
        if self.title == "LowConc PAINT R1" and self.panel_var.get() == "D-Biotin":
            return
        path = filedialog.askopenfilename(title="Choose replacement CSV", filetypes=[("CSV files", "*.csv")])
        if path:
            self.path_var.set(path)
            self.refresh()

    def replacement_sources(self, names):
        """Route the LowConc D-Biotin panel to its physical 20 µM base plate."""
        biotin = [name for name in names if self.title == "LowConc PAINT R1" and name.startswith("D-Biotin_")]
        primary = [name for name in names if name not in biotin]
        sources = []
        if primary:
            sources.append((Path(self.path_var.get()), self.plate_name, self.concentration_nm, primary))
        if biotin:
            sources.append((SHEET_DIR / "lowconc_D_biotin_additions.csv", "SourcePlate8[8]", 20000.0, biotin))
        return sources

    def refresh(self) -> None:
        self._clear_panel_cache()
        self.variables.clear()
        self.smart_absorbed.clear()
        self.site_fractions.clear()
        self.site_concentrations_nm.clear()
        self.buttons.clear()
        self.circle_sites.clear()
        self.color_frames.clear()
        self.selectable.clear()
        self.conflicts.clear()
        try:
            self.available = replacement_names(Path(self.path_var.get()))
            if self.title == "LowConc PAINT R1":
                self.available.update(replacement_names(SHEET_DIR / "lowconc_D_biotin_additions.csv"))
        except Exception as exc:
            self.available = set()
            for child in self.panel_host.winfo_children():
                child.destroy()
            ttk.Label(self.panel_host, text="Cannot read sheet: {}".format(exc), foreground="#a40000").pack()
            return
        for panel in self.panels:
            active = panel.get("active")
            for row in panel["rows"]:
                for column in panel["columns"]:
                    position = (str(row), str(column))
                    if active is not None and position not in active:
                        continue
                    name = panel_sequence_name(panel, row, column)
                    self.variables[name] = tk.BooleanVar(value=False)
                    self.selectable[name] = name in self.available
        self._apply_encoding()
        self._show_panel()

    def _clear_panel_cache(self) -> None:
        for child in self.panel_host.winfo_children():
            child.destroy()
        self._panel_cache.clear()

    def _show_panel(self) -> None:
        if self.title == "Aptamers":
            self._show_aptamer_panels()
            return
        for child in self.panel_host.winfo_children():
            child.pack_forget()
        panel = self._current_panel()
        biotin = self.title == "LowConc PAINT R1" and panel["label"] == "D-Biotin"
        self.sheet_path_entry.configure(textvariable=self.biotin_sheet_path if biotin else self.path_var,
                                        state="readonly" if biotin else "normal")
        self.sheet_browse_button.configure(state="disabled" if biotin else "normal")
        self.source_plate_label.set("SourcePlate8[8]" if biotin else self.plate_name)
        pitch = "{:.4g} nm columns; {:.4g} nm rows".format(
            EXTENSION_COLUMN_SPACING_NM,
            EXTENSION_ROW_SPACING_NM,
        )
        if self.title == "Aptamers":
            orientation = "12 × 8 body grid plus outer edge sites: {}".format(pitch)
        elif panel.get("coordinate_system") == "R1 replacement wells":
            orientation = "Page {} · matched R1 replacement wells · columns 12 → 1".format(panel["page"][1:])
        elif panel.get("mirror_columns", False):
            orientation = "U physical orientation: {} mirrored (12 → 1)".format(pitch)
        elif list(panel["columns"]) == COLS:
            orientation = "D physical orientation: {} (1 → 12)".format(pitch)
        else:
            orientation = "Columns shown in listed order"
        if panel.get("spacing_x_nm") is not None:
            orientation += "; extra C6–C7 gap: {:g} nm; circle radius: {:g} nm".format(EXTENSION_EXTRA_COLUMN_GAP_NM, self.circle_radius_nm)
        self.orientation_summary.set(orientation)
        key = str(panel["label"])
        cached = self._panel_cache.get(key)
        if cached is None:
            self.buttons = {}
            self.circle_sites = {}
            self.color_frames = {}
            self._build_panel(panel, self.available)
            frame = self.panel_host.winfo_children()[-1]
            self._panel_cache[key] = (frame, self.buttons, self.circle_sites, self.color_frames)
        else:
            frame, self.buttons, self.circle_sites, self.color_frames = cached
            frame.pack(fill="x", pady=5)
            for name in self.buttons.keys() | self.circle_sites.keys():
                self._paint_square(name)
        self._update_selection_summary()

    def _show_aptamer_panels(self) -> None:
        family = self.panel_var.get().split("-", 1)[0]
        right_labels = [str(panel["label"]) for panel in self.panels
                        if str(panel["label"]).startswith(family + "-")
                        and panel["label"] != family + "-Apt"]
        self.right_panel_picker.configure(values=right_labels)
        if self.right_panel_var.get() not in right_labels:
            self.right_panel_var.set(right_labels[0])
        for child in self.panel_host.winfo_children():
            child.pack_forget()
        visible_circles = {}
        for label in (self.panel_var.get(), self.right_panel_var.get()):
            panel = next(item for item in self.panels if item["label"] == label)
            cached = self._panel_cache.get(label)
            if cached is None:
                self.buttons, self.circle_sites, self.color_frames = {}, {}, {}
                self._build_panel(panel, self.available)
                frame = self.panel_host.winfo_children()[-1]
                cached = (frame, self.buttons, self.circle_sites, self.color_frames)
                self._panel_cache[label] = cached
            frame, _, circles, _ = cached
            frame.pack(side="left", anchor="n", padx=(0, 8), pady=5)
            visible_circles.update(circles)
        self.buttons, self.circle_sites, self.color_frames = {}, visible_circles, {}
        for name in self.circle_sites:
            self._paint_square(name)
        self.orientation_summary.set("Left 01–06 · Right 07–12 · outer edge sites included")
        self._update_selection_summary()

    def _current_panel(self) -> Dict[str, object]:
        selected_label = self.panel_var.get()
        return next((item for item in self.panels if str(item["label"]) == selected_label), self.panels[0])

    def _request_template(self, panel=None) -> None:
        panel = self._current_panel() if panel is None else panel
        selected_names = set(self.selected()) | self.smart_absorbed
        selected_positions = panel_template_positions(panel, selected_names)
        if not selected_positions:
            messagebox.showinfo(
                "No sites selected",
                "Select at least one position in {} before making its template.".format(panel["label"]),
                parent=self.panel_host,
            )
            return
        self.template_requested(self.title, panel, selected_positions)

    def _build_panel(self, panel: Dict[str, object], available: Set[str]) -> None:
        label = str(panel["label"])
        frame = ttk.LabelFrame(self.panel_host, text=label)
        frame.pack(fill="x", pady=5)
        if self.title == "Aptamers":
            self._build_physical_panel(frame, aptamer_display_panel(panel, half=True), available)
            return
        rows = list(panel["rows"])
        columns = panel_display_columns(panel)
        active = panel.get("active")
        colors = panel.get("colors", {})
        spacing_x_nm = panel.get("spacing_x_nm")
        spacing_y_nm = panel.get("spacing_y_nm")
        physical_layout = spacing_x_nm is not None and spacing_y_nm is not None
        if physical_layout:
            self._build_physical_panel(frame, panel, available)
            return
        unavailable_in_panel = []

        header_row = 0
        ttk.Label(frame, text="").grid(row=header_row, column=0, padx=3)
        for ci, column in enumerate(columns, 1):
            if physical_layout:
                frame.grid_columnconfigure(ci, minsize=int(round(float(spacing_x_nm) * 5.0)))
            ttk.Label(frame, text=str(column), anchor="center").grid(row=header_row, column=ci, padx=3)
        for ri, row in enumerate(rows, 1):
            grid_row = header_row + ri
            if physical_layout:
                frame.grid_rowconfigure(grid_row, minsize=int(round(float(spacing_y_nm) * 5.0)))
            ttk.Label(frame, text=str(row), width=6, anchor="e").grid(row=grid_row, column=0, padx=(4, 8))
            for ci, column in enumerate(columns, 1):
                position = (str(row), str(column))
                name = panel_sequence_name(panel, row, column)
                configured = active is None or position in active
                exists = name in available
                is_selectable = configured and exists
                variable = self.variables.get(name)
                if variable is None:
                    variable = tk.BooleanVar(value=False)
                color = colors.get(position) if isinstance(colors, dict) else None
                color_frame = None
                button_parent = frame
                if color:
                    color_frame = tk.Frame(frame, background=str(color), padx=1, pady=1)
                    color_frame.grid(
                        row=grid_row,
                        column=ci,
                        padx=0 if physical_layout else (3, 12 if ci == 6 else 3),
                        pady=0 if physical_layout else 2,
                    )
                    button_parent = color_frame
                button = tk.Checkbutton(
                    button_parent,
                    text="" if is_selectable else "×",
                    variable=variable,
                    indicatoron=False,
                    width=9,
                    height=1,
                    relief="raised" if is_selectable else "flat",
                    borderwidth=1,
                    highlightthickness=0 if color else 1,
                    highlightbackground="#4c8f65" if is_selectable else "#a5aaae",
                    highlightcolor="#246b3d",
                    background=self.SELECTABLE_BG if is_selectable else self.UNAVAILABLE_BG,
                    activebackground=self.SELECTABLE_HOVER_BG if is_selectable else self.UNAVAILABLE_BG,
                    foreground="#222222" if is_selectable else "#62676b",
                    disabledforeground="#62676b",
                    selectcolor=self.SELECTED_BG,
                    state="normal" if is_selectable else "disabled",
                )
                button.configure(command=lambda item=name: self._on_square_toggled(item))
                if color_frame is not None:
                    button.pack()
                    if configured:
                        self.color_frames[name] = color_frame
                else:
                    button.grid(
                        row=grid_row,
                        column=ci,
                        padx=0 if physical_layout else (3, 12 if ci == 6 else 3),
                        pady=0 if physical_layout else 2,
                    )
                if configured:
                    self.buttons[name] = button
                    if not is_selectable:
                        unavailable_in_panel.append(name)
                    self._paint_square(name)
                if is_selectable:
                    button.bind("<Enter>", lambda _event, item=name: self._hover_square(item, True))
                    button.bind("<Leave>", lambda _event, item=name: self._hover_square(item, False))
        if unavailable_in_panel:
            ttk.Label(
                frame,
                text="Grey wells are unavailable in this CSV ({} position{}).".format(
                    len(unavailable_in_panel), "" if len(unavailable_in_panel) == 1 else "s"
                ),
                foreground="#666666",
            ).grid(row=header_row + len(rows) + 1, column=0, columnspan=len(columns) + 1, sticky="w", padx=5, pady=3)

    def _build_physical_panel(self, frame, panel, available):
        # Use one scale for both axes; overlapping masks remain independently
        # selectable by choosing the nearest site center on a click.
        scale, radius = 6.0, self.circle_radius_nm
        columns = panel_display_columns(panel)
        rows = list(panel["rows"])
        sites = panel_circle_centers(panel)
        extra_sites = panel.get("extra_sites", [])
        sites += extra_sites
        left = 40.0 + radius * scale - min(0.0, min(x for _, x, _ in sites)) * scale
        top = 30.0 + radius * scale - min(0.0, min(y for _, _, y in sites)) * scale
        width = left + max(x for _, x, _ in sites) * scale + radius * scale + 15
        height = top + max(y for _, _, y in sites) * scale + radius * scale + 15
        canvas = tk.Canvas(frame, width=width, height=height,
                           background="#15191d", highlightthickness=0)
        canvas.pack(anchor="w", padx=5, pady=5)
        for ci, column in enumerate(columns):
            canvas.create_text(left + sites[ci][1] * scale, 15,
                               text=str(column), fill="white")
        for ri, row in enumerate(rows):
            canvas.create_text(8, top + ri * float(panel["spacing_y_nm"]) * scale,
                               text=str(row), fill="white", anchor="w")
        active = panel.get("active")
        colors = panel.get("colors", {})
        targets = []
        for index, (name, x_nm, y_nm) in enumerate(sites):
            if index < len(rows) * len(columns):
                ri, ci = divmod(index, len(columns))
                position = (str(rows[ri]), str(columns[ci]))
                configured = active is None or position in active
            else:
                position = None
                configured = True
            selectable = configured and name in available
            x, y, r = left + x_nm * scale, top + y_nm * scale, radius * scale
            color = str(colors.get(position, "#34c6d3")) if selectable else "#62676b"
            ring = canvas.create_oval(x-r, y-r, x+r, y+r, outline=color, width=1.5)
            dot = canvas.create_oval(x-4, y-4, x+4, y+4, fill=color, outline="")
            if configured:
                canvas.create_text(x, y, text="", fill="white", font=("TkDefaultFont", 9, "bold"),
                                   tags=("concentration_labels", "target_{}".format(ring)))
                self.circle_sites[name] = (canvas, ring, dot, color)
                self._paint_square(name)
            if not selectable:
                canvas.create_text(x, y, text="×", fill="#969ba0")
            targets.append((name, x_nm, y_nm, selectable))
        canvas.tag_raise("concentration_labels")
        def toggle(event):
            name = nearest_panel_site(targets, (event.x-left)/scale, (event.y-top)/scale, radius_nm=radius)
            if name is not None and name not in self.encoded_sites:
                self.variables[name].set(not self.variables[name].get())
                self._on_square_toggled(name)
        canvas.bind("<Button-1>", toggle)
        help_text = "Click near a site center to select it. Filled circles are selected; gray × sites are unavailable. Circles overlap at their physical spacing."
        ttk.Label(frame, text=help_text,
                  wraplength=int(width) if extra_sites else 0).pack(anchor="w", padx=5)

    @staticmethod
    def _legend_square(parent: tk.Widget, background: str, text: str, foreground: str = "#222222") -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            width=2,
            height=1,
            relief="solid",
            borderwidth=1,
            background=background,
            foreground=foreground,
        )

    def _paint_square(self, name: str) -> None:
        circle = self.circle_sites.get(name)
        if circle is not None:
            canvas, ring, dot, color = circle
            selected = self.variables[name].get()
            highlight = self._selected_highlight(name, canvas.cget("background"), self.selection_color)
            color = self.CONFLICT_BG if selected and name in self.conflicts else (highlight if selected else color)
            canvas.itemconfigure(ring, outline=color, fill=highlight if selected else "",
                                 width=3 if selected else 1.5)
            canvas.itemconfigure(dot, fill=color, state="hidden" if selected else "normal")
            canvas.itemconfigure("target_{}".format(ring), text=self._target_label(name) if selected else "",
                                 fill="black" if selected and self.site_fractions.get(name, 1.0) >= 0.5 else "white")
            return
        button = self.buttons.get(name)
        variable = self.variables.get(name)
        if button is None or variable is None or not self.selectable.get(name, False):
            return
        selected = variable.get()
        conflict = selected and name in self.conflicts
        highlight = self._selected_highlight(name, self.SELECTABLE_BG, self.SELECTED_BG)
        button.configure(
            text=(("× " if conflict else "") + self._target_label(name)) if selected else "",
            background=self.CONFLICT_BG if conflict else (highlight if selected else self.SELECTABLE_BG),
            activebackground=self.CONFLICT_BG if conflict else (highlight if selected else self.SELECTABLE_HOVER_BG),
            selectcolor=self.CONFLICT_BG if conflict else highlight,
            foreground="#ffffff" if selected else "#222222",
            relief="sunken" if selected else "raised",
        )

    def _on_square_toggled(self, name: str) -> None:
        if self.variables[name].get():
            if self.replacement_percentage.get() not in self._fraction_options:
                self.variables[name].set(False)
                self._paint_square(name)
                return
            override = self.new_selection_concentration.get().strip()
            try:
                target = float(override or self.desired_concentration_nm.get())
                desired_concentration_ratio(target, self.base_desired_concentration_nm)
            except ValueError:
                self.variables[name].set(False)
                self.replacement_error.set("Enter a finite positive concentration before selecting sites.")
                self._paint_square(name)
                return
            if override:
                self.site_concentrations_nm[name] = target
            else:
                self.site_concentrations_nm.pop(name, None)
            self.site_fractions[name] = self._fraction_options[self.replacement_percentage.get()]
        else:
            self.site_fractions.pop(name, None)
            self.site_concentrations_nm.pop(name, None)
        if self.title in COLOR_BITS:
            self.encoding_enabled.set(True)
            self._apply_encoding()
        self.mark_conflicts(set())
        self._paint_square(name)
        self._update_selection_summary()
        if self.selection_changed is not None:
            self.selection_changed()

    def _hover_square(self, name: str, entering: bool) -> None:
        button = self.buttons.get(name)
        variable = self.variables.get(name)
        if button is None or variable is None or variable.get() or not self.selectable.get(name, False):
            return
        button.configure(background=self.SELECTABLE_HOVER_BG if entering else self.SELECTABLE_BG)

    def raw_selected(self) -> List[str]:
        """Explicit UI choices, including R1 supplied by a combined lock."""
        if self.title in COLOR_BITS and not self.encoding_enabled.get():
            return []
        return [name for name, variable in self.variables.items() if variable.get()]

    def selected(self) -> List[str]:
        """Physical sources to generate, excluding R1 supplied by Smart locks."""
        return [name for name in self.raw_selected() if name not in self.smart_absorbed]

    def _update_selection_summary(self) -> None:
        count = len(self.raw_selected())
        suffix = " ({} via Smart common locks)".format(len(self.smart_absorbed)) if self.smart_absorbed else ""
        self.selection_summary.set("{} selected in this set{}".format(count, suffix))

    def mark_conflicts(self, names: Set[str]) -> None:
        changed = self.conflicts.symmetric_difference(names)
        self.conflicts = set(names)
        for name in changed & (self.buttons.keys() | self.circle_sites.keys()):
            self._paint_square(name)

    def clear(self) -> None:
        self.encoding_enabled.set(False)
        self.x_shift.set(next(iter(SHIFT_STATES)))
        self.y_shift.set(next(iter(SHIFT_STATES)))
        self.conflicts.clear()
        self.smart_absorbed.clear()
        self.site_fractions.clear()
        self.site_concentrations_nm.clear()
        for name, variable in self.variables.items():
            variable.set(False)
            self._paint_square(name)
        self._apply_encoding()
        self._update_selection_summary()
        if self.selection_changed is not None:
            self.selection_changed()


class SmartMiRNASetView(SetView):
    """Keep the user's lock selection while resolving its physical source variant."""

    def __init__(self, parent, title, csv_path, plate_name, panels, **kwargs):
        self.smart_variants = {name: variant for panel in panels
                               for name, variant in panel.get("smart_variants", {}).items()}
        self.smart_combined = set()
        self.smart_status = tk.StringVar(master=parent)
        super().__init__(parent, title, csv_path, plate_name, panels, **kwargs)
        self._update_smart_status()

    def refresh(self):
        self.smart_combined.clear()
        super().refresh()
        self._update_smart_status()

    def resolved_name(self, name):
        return self.smart_variants[name]["name"] if name in self.smart_combined else name

    def _sync_smart_metadata(self):
        for plain, variant in self.smart_variants.items():
            dual = variant["name"]
            if plain in self.smart_combined:
                self.site_fractions[dual] = self.site_fractions.get(plain, 1.0)
                if plain in self.site_concentrations_nm:
                    self.site_concentrations_nm[dual] = self.site_concentrations_nm[plain]
                else:
                    self.site_concentrations_nm.pop(dual, None)
            else:
                self.site_fractions.pop(dual, None)
                self.site_concentrations_nm.pop(dual, None)

    def selected(self):
        self._sync_smart_metadata()
        return [self.resolved_name(name) for name in super().selected()]

    def upgrade_common_lock(self, plain):
        variant = self.smart_variants[plain]
        if variant["name"] not in self.available:
            raise ValueError("Combined R1–lock staple is missing from the miRNA sheet: " + variant["name"])
        self.smart_combined.add(plain)
        self._sync_smart_metadata()
        self._paint_square(plain)
        self._update_smart_status()

    def _update_smart_status(self):
        conversions = ["{} → {} (lock + R1)".format(self.smart_variants[name]["source_well"],
                        self.smart_variants[name]["combined_well"]) for name in sorted(self.smart_combined)]
        self.smart_status.set(
            "Smart common locks: " + ("; ".join(conversions) if conversions else "plain locks by default") +
            ". Overlapping PAINT R1 / LowConc PAINT R1 selections are replaced by the combined staple, using the common lock's concentration and percentage. "
            "R1 sites remain visibly selected. Deselect R1 to use the plain lock; deselect the lock to use standalone R1.")

    def _on_square_toggled(self, name):
        if name in self.smart_combined and not self.variables[name].get():
            self.smart_combined.remove(name)
            self._sync_smart_metadata()
        super()._on_square_toggled(name)
        self._update_smart_status()

    def mark_conflicts(self, names):
        inverse = {variant["name"]: plain for plain, variant in self.smart_variants.items()}
        super().mark_conflicts({inverse.get(name, name) for name in names})

    def _request_template(self, panel=None):
        panel = dict(self._current_panel() if panel is None else panel)
        panel["sequence_names"] = {site: self.resolved_name(name) for site, name in panel["sequence_names"].items()}
        super()._request_template(panel)

    def clear(self):
        self.smart_combined.clear()
        super().clear()
        self._update_smart_status()


def panel_sequence_name(panel: Dict[str, object], row: object, column: object) -> str:
    if "sequence_names" in panel:
        return panel["sequence_names"].get((str(row), str(column)), "")
    template = str(panel.get("name_template", "{label}_{row}-{column}"))
    return template.format(label=panel["label"], row=row, column=column)


def panel_display_columns(panel: Dict[str, object]) -> List[object]:
    """Return columns in their physical left-to-right display order."""
    columns = list(panel["columns"])
    return list(reversed(columns)) if panel.get("mirror_columns", False) else columns


def panel_circle_centers(panel):
    """Physical centers in displayed order, matching panel template exports."""
    columns = panel_display_columns(panel)
    offsets = [0.0 if i < 6 else EXTENSION_EXTRA_COLUMN_GAP_NM for i in range(len(columns))]
    xs = template_column_positions(len(columns), float(panel["spacing_x_nm"]),
                                   {"column_offsets_nm": offsets})
    return [(panel_sequence_name(panel, row, column), float(xs[ci]),
             ri * float(panel["spacing_y_nm"]))
            for ri, row in enumerate(panel["rows"])
            for ci, column in enumerate(columns)]


def nearest_panel_site(sites, x, y, radius_nm=7.5):
    """Choose the nearest center, including unavailable sites to avoid misclicks."""
    if not sites:
        return None
    name, sx, sy, selectable = min(sites, key=lambda site: (site[1]-x)**2 + (site[2]-y)**2)
    return name if selectable and (sx-x)**2 + (sy-y)**2 <= radius_nm**2 else None


def panel_template_positions(
    panel: Dict[str, object], selected_names: Iterable[str]
) -> List[Tuple[int, int]]:
    """Map selected sequences into the panel's physical displayed orientation."""
    selected = set(selected_names)
    return [
        (row_index, column_index)
        for row_index, row in enumerate(panel["rows"])
        for column_index, column in enumerate(panel_display_columns(panel))
        if panel_sequence_name(panel, row, column) in selected
    ]


def aptamer_display_panel(panel, half=False):
    """Place aptamer anchors on the body lattice and its outer edge staples.

    Body columns advance 32 bases. Edge staples are at -3/470 bases,
    versus 41/441 for the first/last body columns; edge helix pairs
    0/1, 8/9, 16/17 lie half a row above the odd-helix body pairs.
    Preserve the original CSV names and selection/export definitions.
    """
    label = str(panel["label"])
    left = label in {"PDGF-Apt", "Kana-Apt"}
    names = {
        ("H07", "05" if left else "08"): label + ("_Mid-L2" if left else "_Mid-R2"),
        ("H07", "06" if left else "07"): label + ("_Mid-L1" if left else "_Mid-R1"),
    }
    display = dense_panel(label, active=set(names))
    if half:
        display["columns"] = COLS[:6] if left else COLS[6:]
    display["sequence_names"] = names
    pitch_x, pitch_y = EXTENSION_COLUMN_SPACING_NM, EXTENSION_ROW_SPACING_NM
    edge_x = (-44 / 32 * pitch_x if left else
              11 * pitch_x + EXTENSION_EXTRA_COLUMN_GAP_NM + 29 / 32 * pitch_x)
    if half and not left:
        edge_x -= 6 * pitch_x + EXTENSION_EXTRA_COLUMN_GAP_NM
    display["extra_sites"] = [
        (label + "_" + row + ("-L3" if left else "-R3"), edge_x, offset * pitch_y)
        for row, offset in (("Top", -0.5), ("Mid", 3.5), ("Bot", 7.5))
    ]
    return display


def dense_panel(
    label: str,
    active: Optional[Set[Tuple[str, str]]] = None,
    colors=None,
    name_template: Optional[str] = None,
    mirror_columns: bool = False,
) -> Dict[str, object]:
    panel: Dict[str, object] = {
        "label": label,
        "rows": ROWS,
        "columns": COLS,
        "active": active,
        "colors": colors or {},
        "mirror_columns": mirror_columns,
        "spacing_x_nm": EXTENSION_COLUMN_SPACING_NM,
        "spacing_y_nm": EXTENSION_ROW_SPACING_NM,
    }
    if name_template is not None:
        panel["name_template"] = name_template
    return panel


def soyeon_mb_panels() -> List[Dict[str, object]]:
    """Keep the supplied sequence names while displaying their encoded sites."""
    records, _ = parse_source2(SHEET_DIR / "soyeon_MB_replace.csv")
    panels = []
    for prefix, label, rows in (
        ("D_MB", "Soyeon D-MB", ROWS),
        ("DL_", "Soyeon DL", ["00", "13"]),
    ):
        names = {}
        for record in records:
            match = re.fullmatch(r"" + prefix + r"(\d{2})_(\d{2})_\[[A-P]\d{2}\]", record["Name"])
            if match:
                row, column = match.groups()
                names[("H" + row if prefix == "D_MB" else row, column)] = record["Name"]
        panels.append({
            "label": label, "rows": rows, "columns": COLS,
            "active": set(names), "sequence_names": names,
            "spacing_x_nm": EXTENSION_COLUMN_SPACING_NM,
            "spacing_y_nm": EXTENSION_ROW_SPACING_NM,
        })
    return panels


def mirna_panels() -> List[Dict[str, object]]:
    """Keep page-local names, and place each staple at its matching R1 base well."""
    records, _ = parse_source2(SHEET_DIR / "miRNA_replace.csv")
    positions = replacement_positions(SHEET_DIR)
    panels = []

    def add_panel(label, sequence_names, orange=False):
        names = {}
        pages = set()
        for name in sequence_names:
            site = positions[name]
            if site in names:
                raise ValueError("Duplicate miRNA grid position in {}: {}".format(label, site))
            names[site] = name
            pages.add(anchor_position(name)[0])
        if len(pages) != 1:
            raise ValueError("A miRNA panel must refer to one page: " + label)
        panel = dense_panel(label, active=set(names), mirror_columns=True)
        panel["sequence_names"] = names
        panel["page"] = next(iter(pages))
        panel["coordinate_system"] = "R1 replacement wells"
        if orange:
            panel["colors"] = {site: "#e98a28" for site in names}
        panels.append(panel)

    designs = target_designs(SHEET_DIR)
    for target, design in designs.items():
        add_panel(target, design["target_locks"])
    design = next(iter(designs.values()))
    add_panel("Smart common locks (p2)", design["common_locks"], orange=True)
    smart = panels[-1]
    by_name = {row["Name"]: row for row in records}
    smart["smart_variants"] = {
        plain: {"name": dual, "replace_well": by_name[plain]["Replace Well"],
                "source_well": by_name[plain]["Well"], "combined_well": by_name[dual]["Well"]}
        for plain, dual in zip(design["common_locks"], design["combined_locks"])
    }
    smart["r1_locations"] = {dual: "{}-{}".format(*positions[dual]) for dual in design["combined_locks"]}
    for group, label in (("p1", "p1 — Lock 3′ anchors"), ("p2", "p2 — Direct 5′ anchors")):
        add_panel(label, [record["Name"] for record in records
                          if record["Well"].startswith("B") and anchor_position(record["Name"])[0] == group])
    return panels


def panel_definitions() -> List[Tuple[str, str, str, List[Dict[str, object]]]]:
    set_d = {
        ("H01", "01"), ("H01", "12"), ("H05", "04"), ("H05", "09"),
        ("H11", "04"), ("H11", "09"), ("H15", "02"), ("H15", "12"),
        ("H07", "04"), ("H07", "09"), ("H13", "04"), ("H13", "09"),
    }
    set_u = {("H05", "04"), ("H05", "09"), ("H11", "04"), ("H11", "09")}
    set_e = {
        ("H01", "01"), ("H01", "05"), ("H01", "08"), ("H01", "12"),
        ("H15", "01"), ("H15", "05"), ("H15", "08"), ("H15", "12"),
    }
    colors = {}
    palette = ("red", "blue", "green")
    for ri, row in enumerate(ROWS):
        for ci, column in enumerate(COLS):
            colors[(row, column)] = palette[(ci - ri) % 3]
    paint_r1_colors = {
        (row, column): "cyan"
        for row in ROWS
        for column in COLS
    }
    left = {("Top", "L3"), ("Mid", "L3"), ("Bot", "L3"), ("Mid", "L2"), ("Mid", "L1")}
    right = {("Top", "R3"), ("Mid", "R3"), ("Bot", "R3"), ("Mid", "R2"), ("Mid", "R1")}
    c_panels = []
    for label in ("PDGF-Apt", "Kana-Apt"):
        c_panels.append({"label": label, "rows": ["Top", "Mid", "Bot"], "columns": ["L3", "L2", "L1", "R1", "R2", "R3"], "active": left})
    for label in ("PDGF-14", "PDGF-18", "PDGF-22", "PDGF-26", "PDGF-30", "PDGF-34", "PDGF-38", "Kana-14", "Kana-18", "Kana-22"):
        c_panels.append({"label": label, "rows": ["Top", "Mid", "Bot"], "columns": ["L3", "L2", "L1", "R1", "R2", "R3"], "active": right})
    definitions = [
        (
            "Yaritza Extensions",
            "yaritza_replace.csv",
            "SourcePlate3[3]",
            [dense_panel("D-Apt"), dense_panel("U-Apt", mirror_columns=True)],
        ),
        ("Aptamers", "max_replace.csv", "SourcePlate4[4]", c_panels),
        ("Soyeon MB", "soyeon_MB_replace.csv", "SoyeonMB[1]", soyeon_mb_panels()),
        ("miRNA", "miRNA_replace.csv", "SourcePlate10[10]", mirna_panels()),
        (
            "MB",
            "max_replace_MB.csv",
            "SourcePlate4[4]",
            [dense_panel("D-MB", set_d), dense_panel("U-MB", set_u, mirror_columns=True)],
        ),
        (
            "PAINT P1",
            "PAINT_replace.csv",
            "SourcePlate5[5]",
            [dense_panel("D-Biotin", set_e), dense_panel("U-PAINT", colors=colors, mirror_columns=True)],
        ),
        (
            "PAINT R1",
            "PAINT_R1_replace.csv",
            "SourcePlate6[6]",
            [
                dense_panel(
                    "U-Apt R1",
                    colors=paint_r1_colors,
                    name_template="U-Apt_{row}-{column}_R1",
                    mirror_columns=True,
                )
            ],
        ),
        (
            "LowConc PAINT R1",
            "PAINT_R1_replace.csv",
            "SourcePlate7[7]",
            [dense_panel(
                "U-Apt R1", colors=paint_r1_colors,
                name_template="U-Apt_{row}-{column}_R1", mirror_columns=True,
            )],
        ),
    ]

    _, filename, plate_name, panels = next(item for item in definitions if item[0] == "LowConc PAINT R1")
    definitions.extend((title, filename, plate_name, deepcopy(panels)) for title in R1_SELECTION_COLORS)
    panels.append(dense_panel("D-Biotin", set_e))
    return definitions


class DestinationPlateView(ttk.Frame):
    CELL_WIDTH = 42
    CELL_HEIGHT = 29
    LEFT = 38
    TOP = 32

    def __init__(self, parent: tk.Widget, save_well) -> None:
        super().__init__(parent, padding=10)
        self.save_well = save_well
        self.selected_well = None
        self.current_labels = {}
        self.summary = tk.StringVar()
        self.detail = tk.StringVar(value="Click a well to see its recorded usage.")
        ttk.Label(self, textvariable=self.summary, font=("TkDefaultFont", 11, "bold")).pack(fill="x")
        legend = ttk.Frame(self)
        legend.pack(fill="x", pady=(5, 8))
        for color, label in (
            ("#ffffff", "Unused"),
            ("#9ecae1", "Used"),
            ("#3182bd", "At capacity"),
            ("#fff2a8", "Currently entered"),
            ("#f3a35c", "Entered + previously used"),
        ):
            tk.Label(legend, width=2, background=color, relief="solid", borderwidth=1).pack(side="left")
            ttk.Label(legend, text=" " + label).pack(side="left", padx=(0, 13))
        canvas_host = ttk.Frame(self)
        canvas_host.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(canvas_host, background="#f5f5f5", highlightthickness=0)
        xbar = ttk.Scrollbar(canvas_host, orient="horizontal", command=self.canvas.xview)
        ybar = ttk.Scrollbar(canvas_host, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        canvas_host.rowconfigure(0, weight=1)
        canvas_host.columnconfigure(0, weight=1)
        ttk.Label(self, textvariable=self.detail, padding=(2, 7)).pack(fill="x")
        editor = ttk.Frame(self)
        editor.pack(fill="x")
        self.selection_text = tk.StringVar(value="Select a well")
        self.label_var = tk.StringVar()
        self.used_var = tk.BooleanVar()
        ttk.Label(editor, textvariable=self.selection_text, width=16).pack(side="left")
        ttk.Label(editor, text="Label:").pack(side="left", padx=(0, 5))
        self.label_entry = ttk.Entry(editor, textvariable=self.label_var, width=32, state="disabled")
        self.label_entry.pack(side="left")
        self.used_check = ttk.Checkbutton(editor, text="Used", variable=self.used_var, state="disabled")
        self.used_check.pack(side="left", padx=10)
        self.save_button = ttk.Button(editor, text="Save well", command=self._save_selected, state="disabled")
        self.save_button.pack(side="left")
        ttk.Label(self, text="Uncheck Used to clear recorded transfers and make the well available again. Leave Label blank to show its address.").pack(fill="x", pady=(5, 0))
        self.canvas.bind("<Button-1>", self._clicked)
        self.current_plate = ""
        self.current_wells: Dict[str, Dict] = {}
        self.capacity_nl = 12500

    def render(self, plate_name: str, state: Dict, entered_wells: Sequence[str], capacity_ul: float) -> None:
        if plate_name != self.current_plate:
            self.selected_well = None
        self.current_plate = plate_name
        self.current_wells = plate_wells(state, plate_name)
        self.current_labels = state["plates"][plate_name].get("labels", {})
        self.capacity_nl = max(1, int(capacity_ul * 1000))
        entered = set(entered_wells)
        used_count = len(self.current_wells)
        unused_count = len(all_384_wells()) - used_count
        total_ul = sum(int(item.get("volume_nL", 0)) for item in self.current_wells.values()) / 1000.0
        self.summary.set(
            "{} — {} used wells, {} unused wells, {:.3f} µL recorded".format(
                plate_name, used_count, unused_count, total_ul
            )
        )
        self.canvas.delete("all")
        for column in range(1, 25):
            x = self.LEFT + (column - 1) * self.CELL_WIDTH
            self.canvas.create_text(x + self.CELL_WIDTH / 2, 16, text="{:02d}".format(column))
        for row_index, row in enumerate(PLATE_ROWS):
            y = self.TOP + row_index * self.CELL_HEIGHT
            self.canvas.create_text(20, y + self.CELL_HEIGHT / 2, text=row, font=("TkDefaultFont", 9, "bold"))
            for column in range(1, 25):
                well = "{}{:02d}".format(row, column)
                x = self.LEFT + (column - 1) * self.CELL_WIDTH
                usage = self.current_wells.get(well)
                label = self.current_labels.get(well, well)
                if well in entered and usage:
                    fill = "#f3a35c"
                elif well in entered:
                    fill = "#fff2a8"
                elif usage and int(usage.get("volume_nL", 0)) >= self.capacity_nl:
                    fill = "#3182bd"
                elif usage:
                    fill = "#9ecae1"
                else:
                    fill = "#ffffff"
                self.canvas.create_rectangle(
                    x + 2,
                    y + 2,
                    x + self.CELL_WIDTH - 2,
                    y + self.CELL_HEIGHT - 2,
                    fill=fill,
                    outline="#d62728" if well == self.selected_well else "#59636b",
                    width=2 if well == self.selected_well else 1,
                    tags=("well", well),
                )
                self.canvas.create_text(
                    x + self.CELL_WIDTH / 2,
                    y + self.CELL_HEIGHT / 2,
                    text=label[:5] + "…" if len(label) > 6 else label,
                    font=("TkDefaultFont", 7),
                    tags=("well", well),
                )
        width = self.LEFT + 24 * self.CELL_WIDTH + 8
        height = self.TOP + 16 * self.CELL_HEIGHT + 8
        self.canvas.configure(scrollregion=(0, 0, width, height))
        self._load_selected()

    def _clicked(self, event) -> None:
        column = int((self.canvas.canvasx(event.x) - self.LEFT) // self.CELL_WIDTH) + 1
        row_index = int((self.canvas.canvasy(event.y) - self.TOP) // self.CELL_HEIGHT)
        if column not in range(1, 25) or row_index not in range(16):
            return
        well = "{}{:02d}".format(PLATE_ROWS[row_index], column)
        self.selected_well = well
        for item in self.canvas.find_withtag("well"):
            if self.canvas.type(item) == "rectangle":
                selected = well in self.canvas.gettags(item)
                self.canvas.itemconfigure(item, outline="#d62728" if selected else "#59636b", width=2 if selected else 1)
        self._load_selected()

    def _save_selected(self) -> None:
        if self.selected_well:
            self.save_well(self.current_plate, self.selected_well, self.label_var.get(), self.used_var.get())

    def _load_selected(self) -> None:
        well = self.selected_well
        for widget in (self.label_entry, self.used_check, self.save_button):
            widget.configure(state="normal" if well else "disabled")
        self.selection_text.set(well or "Select a well")
        self.label_var.set(self.current_labels.get(well, ""))
        self.used_var.set(well in self.current_wells)
        if not well:
            self.detail.set("Click a well to edit its label and recorded usage.")
            return
        usage = self.current_wells.get(well)
        if not usage:
            self.detail.set("{} is unused on {}.".format(well, self.current_plate))
            return
        self.detail.set(
            "{} on {}: {} transfers, {:.3f} µL recorded; last used {}".format(
                well,
                self.current_plate,
                int(usage.get("transfer_count", 0)),
                int(usage.get("volume_nL", 0)) / 1000.0,
                usage.get("last_used_utc", "unknown"),
            )
        )


class OrigamiTemplateView(ttk.Frame):
    """Interactive editor for bright-on-dark origami barcode templates."""

    def __init__(self, parent: tk.Widget, initial_directory: Path) -> None:
        super().__init__(parent, padding=12)
        self.initial_directory = Path(initial_directory)
        self.site_variables: Dict[Tuple[int, int], tk.BooleanVar] = {}
        self.site_buttons: Dict[Tuple[int, int], ttk.Button] = {}
        self.logical_stroke_variables: Dict[str, tk.BooleanVar] = {}
        self.logical_schema: Optional[Dict[str, object]] = empty_logical_bit_schema(8, 12)
        self.selected_group_id: Optional[str] = None
        self.selected_group_ids: List[str] = []
        self.group_name_var = tk.StringVar()
        self.group_brightness_var = tk.StringVar(value="1")
        self.group_dark_radius_var = tk.StringVar(value="5")
        self.group_role_var = tk.StringVar(value="Digital bit")
        self.group_active_var = tk.BooleanVar(value=True)
        self._built_shape = (8, 12)
        self.rows_var = tk.StringVar(value="8")
        self.columns_var = tk.StringVar(value="12")
        self.spacing_x_var = tk.StringVar(value=str(EXTENSION_COLUMN_SPACING_NM))
        self.spacing_y_var = tk.StringVar(value=str(EXTENSION_ROW_SPACING_NM))
        self.column_gap_var = tk.StringVar(value=str(EXTENSION_EXTRA_COLUMN_GAP_NM))
        self.margin_var = tk.StringVar(value="20")
        self.sigma_var = tk.StringVar(value="1.5")
        self.width_var = tk.StringVar(value="500")
        self.source_name = tk.StringVar(value="New custom digital-bit encoding")
        self.status = tk.StringVar()
        self._build()
        self._rebuild_grid(select_all=False)

    def _build(self) -> None:
        style = ttk.Style(self)
        for name, background, foreground in (
            ("Lattice.TButton", "#e8edf1", "#35424e"),
            ("SelectedLattice.TButton", "#126b35", "#ffffff"),
        ):
            style.configure(name, background=background, foreground=foreground,
                            font=("TkDefaultFont", 11, "bold"), padding=(3, 2),
                            borderwidth=3, relief="raised")
            style.map(name, background=[("pressed", background), ("active", background)],
                      foreground=[("pressed", foreground), ("active", foreground)])
        style.map("BitGroups.Treeview", background=[("selected", "#164f86")],
                  foreground=[("selected", "#ffffff")])
        ttk.Label(
            self,
            text="Build a barcode image for Origami → Identify Origami → Custom template.",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(fill="x")
        ttk.Label(
            self,
            text="Select lattice sites on the right, then create or update a group on the left.",
        ).pack(fill="x", pady=(3, 6))
        ttk.Label(self, textvariable=self.source_name, foreground="#3d626f").pack(fill="x", pady=(0, 8))
        ttk.Label(self, textvariable=self.status, wraplength=880).pack(side="bottom", fill="x", pady=(8, 0))

        file_actions = ttk.Frame(self)
        file_actions.pack(fill="x", pady=(0, 6))
        for label, command in (("Save template PNG…", self._save),
                               ("Load bit schema JSON…", self._load_logical_schema),
                               ("Save bit schema JSON…", self._save_logical_schema)):
            ttk.Button(file_actions, text=label, command=command).pack(side="left", padx=(0, 6))

        ttk.Button(file_actions, text="Apply to RGB Image", command=self._apply_to_rgb).pack(side="left", padx=(0, 6))

        self.editor_split = tk.PanedWindow(
            self, orient="vertical", sashwidth=8, sashrelief="raised",
            borderwidth=0, background="#d4d8dc",
        )
        self.editor_split.pack(fill="both", expand=True)
        editor_host = ttk.Frame(self.editor_split)
        self.editor_split.add(editor_host, minsize=140, stretch="always")
        editor_host.rowconfigure(0, weight=1)
        editor_host.columnconfigure(0, weight=1)
        self.editor_canvas = tk.Canvas(editor_host, highlightthickness=0, height=380)
        self.editor_canvas.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(editor_host, orient="vertical", command=self.editor_canvas.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(editor_host, orient="horizontal", command=self.editor_canvas.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.editor_canvas.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        body = ttk.Frame(self.editor_canvas)
        editor_window = self.editor_canvas.create_window(0, 0, window=body, anchor="nw")

        def resize_editor(_event=None):
            self.editor_canvas.itemconfigure(
                editor_window, width=max(body.winfo_reqwidth(), self.editor_canvas.winfo_width())
            )
            self.editor_canvas.configure(scrollregion=self.editor_canvas.bbox("all"))

        body.bind("<Configure>", resize_editor)
        self.editor_canvas.bind("<Configure>", resize_editor)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left_tabs = ttk.Notebook(body)
        left_tabs.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        self.logical_frame = ttk.Frame(left_tabs, padding=8)
        controls = ttk.Frame(left_tabs, padding=10)
        left_tabs.add(self.logical_frame, text="Custom digital-bit groups")
        left_tabs.add(controls, text="Geometry")
        fields = (
            ("Grid rows", self.rows_var),
            ("Grid columns", self.columns_var),
            ("Spacing x (nm)", self.spacing_x_var),
            ("Spacing y (nm)", self.spacing_y_var),
            ("Extra gap C6–C7 (nm)", self.column_gap_var),
            ("Image margin (nm)", self.margin_var),
            ("Spot sigma (nm)", self.sigma_var),
            ("PNG width (px)", self.width_var),
        )
        for row, (label, variable) in enumerate(fields):
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(controls, textvariable=variable, width=10)
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=3)
            entry.bind("<Return>", lambda _event: self._settings_changed())
            entry.bind("<FocusOut>", lambda _event: self._settings_changed())
        ttk.Button(controls, text="Apply grid size", command=self._rebuild_grid).grid(
            row=len(fields), column=0, columnspan=2, sticky="ew", pady=(8, 3)
        )
        content = ttk.Frame(body)
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        site_actions = ttk.Frame(content)
        site_actions.grid(row=0, column=0, sticky="ew")
        for label, command in (("Select all", self._select_all),
                               ("Clear selection", self._clear_all), ("Invert selection", self._invert)):
            ttk.Button(site_actions, text=label, command=command).pack(side="left", padx=(0, 6))
        self.site_selection_summary = tk.StringVar()
        ttk.Label(
            content, textvariable=self.site_selection_summary, foreground="#125c30",
            font=("TkDefaultFont", 10, "bold"), padding=(4, 4),
        ).grid(row=1, column=0, sticky="ew")
        self.grid_frame = ttk.LabelFrame(
            content,
            text="Physical lattice sites — PAINT R1 descending column order",
            padding=6,
        )
        self.grid_frame.grid(row=2, column=0, sticky="new")
        preview_host = ttk.LabelFrame(
            self.editor_split, text="Digital bit group preview — drag the divider above to resize", padding=4,
        )
        self.editor_split.add(preview_host, minsize=230, stretch="always")
        self.preview = tk.Canvas(preview_host, background="#202225", highlightthickness=0, height=280)
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Configure>", lambda _event: self._draw_preview())

        def keep_preview_visible(_event=None):
            height = self.editor_split.winfo_height()
            if height > 390:
                x, y = self.editor_split.sash_coord(0)
                self.editor_split.sash_place(0, x, max(140, min(body.winfo_reqheight() + 16, height - 250)))

        self.editor_split.bind("<Configure>", lambda _event: self.after_idle(keep_preview_visible))

    def _grid_shape(self) -> Tuple[int, int]:
        try:
            rows, columns = int(self.rows_var.get()), int(self.columns_var.get())
        except ValueError as exc:
            raise ValueError("Grid rows and columns must be whole numbers.") from exc
        if not 1 <= rows <= 50 or not 1 <= columns <= 50:
            raise ValueError("Grid rows and columns must be between 1 and 50.")
        return rows, columns

    def _settings(self) -> Tuple[int, int, float, float, float, float, int]:
        rows, columns = self._grid_shape()
        try:
            values = (
                rows,
                columns,
                float(self.spacing_x_var.get()),
                float(self.spacing_y_var.get()),
                float(self.margin_var.get()),
                float(self.sigma_var.get()),
                int(self.width_var.get()),
            )
        except ValueError as exc:
            raise ValueError("Spacing, margin, sigma, and PNG width must be numeric.") from exc
        validate_template_settings(*values)
        try:
            gap = float(self.column_gap_var.get())
        except ValueError as exc:
            raise ValueError("The column 6–7 gap must be numeric.") from exc
        if not math.isfinite(gap) or gap < 0:
            raise ValueError("The column 6–7 gap must be finite and nonnegative.")
        if self.logical_schema is not None and (rows, columns) == self._built_shape:
            offsets = list(self.logical_schema.get("column_offsets_nm", [0.0] * columns))
            if len(offsets) != columns:
                raise ValueError("Column offsets must match the grid width.")
            if columns > 6:
                change = gap - (offsets[6] - offsets[5])
                offsets[6:] = [value + change for value in offsets[6:]]
            self.logical_schema["column_offsets_nm"] = offsets
            self.logical_schema["spacing_x_nm"] = values[2]
            self.logical_schema["spacing_y_nm"] = values[3]
        return values

    def _rebuild_grid(
        self,
        select_all: bool = False,
        selected_sites: Optional[Set[Tuple[int, int]]] = None,
    ) -> None:
        try:
            rows, columns = self._grid_shape()
        except ValueError as exc:
            messagebox.showerror("Invalid template grid", str(exc), parent=self)
            return
        old_values = {site: variable.get() for site, variable in self.site_variables.items()}
        self._built_shape = (rows, columns)
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.site_variables.clear()
        self.site_buttons.clear()
        requested_sites = (
            set(selected_sites)
            if selected_sites is not None
            else ({(row, column) for row in range(rows) for column in range(columns)} if select_all else {
                site for site, selected in old_values.items() if selected
            })
        )
        schema = self.logical_schema
        if schema is None or (
            int(schema.get("physical_rows", -1)) != rows
            or int(schema.get("physical_columns", -1)) != columns
        ):
            schema = empty_logical_bit_schema(rows, columns)
            self.logical_schema = schema
            self.selected_group_id = None
            self.selected_group_ids = []
        self._build_logical_controls(schema, schema.get("active_logical_bits", []))
        for column in range(columns):
            ttk.Label(self.grid_frame, text="C{}".format(column + 1), anchor="center").grid(
                row=0, column=columns - column, padx=3, pady=(0, 4)
            )
        for row in range(rows):
            ttk.Label(self.grid_frame, text="R{}".format(row + 1)).grid(row=row + 1, column=0, padx=(0, 6))
            for column in range(columns):
                site = (row, column)
                selected = site in requested_sites
                variable = tk.BooleanVar(value=selected)
                button = ttk.Button(
                    self.grid_frame,
                    width=4,
                    style="SelectedLattice.TButton" if selected else "Lattice.TButton",
                    command=lambda item=site: self._toggle_site(item),
                )
                button.grid(row=row + 1, column=columns - column, padx=2, pady=2)
                self.site_variables[site] = variable
                self.site_buttons[site] = button
                self._paint_site(site)
        self._settings_changed()

    def _build_logical_controls(
        self,
        schema: Optional[Dict[str, object]],
        active_ids: Iterable[str],
    ) -> None:
        for child in self.logical_frame.winfo_children():
            child.destroy()
        self.logical_stroke_variables.clear()
        schema = schema or empty_logical_bit_schema(*self._built_shape)
        active = set(active_ids)
        editor = ttk.Frame(self.logical_frame)
        editor.grid(row=0, column=0, sticky="ew")
        editor.columnconfigure(1, weight=1)
        ttk.Label(editor, text="Group name").grid(row=0, column=0, sticky="w")
        ttk.Entry(editor, textvariable=self.group_name_var, width=24).grid(row=0, column=1, sticky="ew", padx=(8, 12))
        ttk.Label(editor, text="Role").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(
            editor, textvariable=self.group_role_var,
            values=("Digital bit", "Alignment-only", "Dark (alignment)"), state="readonly", width=16,
        ).grid(row=1, column=1, sticky="ew", padx=(8, 12), pady=4)
        ttk.Checkbutton(editor, text="Bit is ON in this template", variable=self.group_active_var).grid(
            row=2, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(editor, text="Brightness factor").grid(row=3, column=0, sticky="w")
        ttk.Entry(editor, textvariable=self.group_brightness_var, width=10).grid(
            row=3, column=1, sticky="ew", padx=(8, 12))
        ttk.Label(editor, text="Digital bits: 1 = normal, 2 = twice the signal, 0.2 = one-fifth.",
                  wraplength=300).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(editor, text="Dark mask radius (nm)").grid(row=5, column=0, sticky="w")
        ttk.Entry(editor, textvariable=self.group_dark_radius_var, width=10).grid(row=5, column=1, sticky="ew")
        ttk.Label(editor, text="Dark groups mark empty regions for alignment, not ON/OFF bits.", wraplength=300).grid(row=6, column=0, columnspan=2, sticky="w")
        for row, buttons in enumerate((
            (("Create group", self._create_group), ("Update group", self._update_group)),
            (("Delete selected", self._delete_group), ("Toggle bit ON/OFF", self._toggle_group_active)),
        ), start=7):
            for column, (label, command) in enumerate(buttons):
                ttk.Button(editor, text=label, command=command).grid(
                    row=row, column=column, sticky="ew", padx=2, pady=3,
                )
        self.logical_frame.columnconfigure(0, weight=1)

        self.group_tree = ttk.Treeview(
            self.logical_frame,
            columns=("color", "name", "role", "sites", "state", "brightness"),
            show="headings",
            height=5,
            selectmode="extended",
            style="BitGroups.Treeview",
        )
        for key, label, width in (
            ("color", "Color", 60),
            ("name", "Group", 105),
            ("role", "Role", 85),
            ("sites", "Sites", 40),
            ("state", "State", 65),
            ("brightness", "Brightness", 75),
        ):
            self.group_tree.heading(key, text=label)
            self.group_tree.column(key, width=width, anchor="w" if key == "name" else "center")
        self.group_tree.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        group_scroll = ttk.Scrollbar(self.logical_frame, orient="vertical", command=self.group_tree.yview)
        group_scroll.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.group_tree.configure(yscrollcommand=group_scroll.set)
        self.group_tree.bind("<<TreeviewSelect>>", self._group_selected)
        if sys.platform == "darwin":
            self.group_tree.bind("<Command-Button-1>", self._command_select_group)
        bit_ids = {str(bit["id"]) for bit in schema.get("logical_bits", [])}
        groups = list(schema.get("alignment_groups", [])) + list(schema.get("logical_bits", [])) + list(schema.get("dark_groups", []))
        for group in groups:
            group_id = str(group["id"])
            is_bit = group_id in bit_ids
            is_dark = group in schema.get("dark_groups", [])
            color = str(group.get("display_color", "#ffffff"))
            self.group_tree.insert(
                "",
                "end",
                iid=group_id,
                tags=(group_id,),
                values=(
                    "● " + color,
                    str(group.get("label", group_id)),
                    "Digital bit" if is_bit else ("Dark" if is_dark else "Alignment-only"),
                    len(group.get("physical_sites", [])),
                    ("ON" if group_id in active else "OFF") if is_bit else ("Dark" if is_dark else "Always on"),
                    f"{group.get('brightness_factor', 1.0):g}×" if is_bit else (f"r={group['mask_radius_nm']:g} nm" if is_dark else "—"),
                ),
            )
            self.group_tree.tag_configure(group_id, foreground=color)
            if is_bit:
                self.logical_stroke_variables[group_id] = tk.BooleanVar(value=group_id in active)
        selected = [group_id for group_id in self.selected_group_ids if self.group_tree.exists(group_id)]
        if selected:
            self.group_tree.selection_set(selected)

    def _logical_strokes_changed(self) -> None:
        if self.logical_schema is not None:
            self.logical_schema["active_logical_bits"] = self._active_bit_ids()
        self._settings_changed()

    def _active_bit_ids(self) -> List[str]:
        if self.logical_stroke_variables:
            return [bit_id for bit_id, variable in self.logical_stroke_variables.items() if variable.get()]
        if self.logical_schema is None:
            return []
        return [str(value) for value in self.logical_schema.get("active_logical_bits", [])]

    def _all_groups(self) -> List[Dict[str, object]]:
        if self.logical_schema is None:
            return []
        return list(self.logical_schema.get("alignment_groups", [])) + list(
            self.logical_schema.get("logical_bits", [])
        ) + list(self.logical_schema.get("dark_groups", []))

    def _group_by_id(self, group_id: Optional[str]) -> Optional[Dict[str, object]]:
        if group_id is None:
            return None
        return next((group for group in self._all_groups() if str(group.get("id")) == group_id), None)

    def _command_select_group(self, event) -> str:
        group_id = self.group_tree.identify_row(event.y)
        if group_id:
            self.group_tree.focus(group_id)
            self.group_tree.selection_toggle(group_id)
        return "break"

    def _group_selected(self, _event: object = None) -> None:
        self.selected_group_ids = list(self.group_tree.selection())
        groups = [self._group_by_id(group_id) for group_id in self.selected_group_ids]
        groups = [group for group in groups if group is not None]
        self.selected_group_id = self.selected_group_ids[0] if len(groups) == 1 else None
        if len(groups) == 1:
            group = groups[0]
            group_id = self.selected_group_id
            self.group_name_var.set(str(group.get("label", group_id)))
            self.group_brightness_var.set(str(group.get("brightness_factor", 1.0)))
            is_bit = group_id in self.logical_stroke_variables
            self.group_role_var.set("Digital bit" if is_bit else ("Dark (alignment)" if group in self.logical_schema.get("dark_groups", []) else "Alignment-only"))
            self.group_dark_radius_var.set(str(group.get("mask_radius_nm", 5.0)))
            self.group_active_var.set(self.logical_stroke_variables[group_id].get() if is_bit else True)
        else:
            self.group_name_var.set("")
            self.group_brightness_var.set("1")
        sites = {(int(row) - 1, int(column) - 1)
                 for group in groups for row, column in group.get("physical_sites", [])}
        for site, variable in self.site_variables.items():
            variable.set(site in sites)
            self._paint_site(site)
        self._settings_changed()

    def _unique_group_id(self, label: str, existing_id: Optional[str] = None) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_") or "bit"
        used = {
            str(group.get("id"))
            for group in self._all_groups()
            if str(group.get("id")) != existing_id
        }
        group_id = base
        suffix = 2
        while group_id in used:
            group_id = f"{base}_{suffix}"
            suffix += 1
        return group_id

    def _create_group(self) -> None:
        self._store_group(create=True)

    def _update_group(self) -> None:
        self._store_group(create=False)

    def _store_group(self, *, create: bool) -> None:
        if not create and len(self.selected_group_ids) != 1:
            messagebox.showerror("Select one group", "Select exactly one group to update its name and sites.", parent=self)
            return
        label = self.group_name_var.get().strip()
        selected = self._selected_sites()
        if not label or not selected:
            messagebox.showerror(
                "Incomplete group",
                "Enter a group name and select at least one physical site.",
                parent=self,
            )
            return
        if not create and self.selected_group_id is None:
            messagebox.showerror("No group selected", "Select the group you want to update.", parent=self)
            return
        is_bit = self.group_role_var.get() == "Digital bit"
        is_dark = self.group_role_var.get() == "Dark (alignment)"
        dark_radius = 5.0
        if is_dark:
            try:
                dark_radius = float(self.group_dark_radius_var.get())
                if not math.isfinite(dark_radius) or dark_radius <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid dark radius", "Enter a positive finite radius in nm.", parent=self)
                return
        brightness_factor = 1.0
        if is_bit:
            try:
                brightness_factor = float(self.group_brightness_var.get())
                if not math.isfinite(brightness_factor) or brightness_factor <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid brightness factor", "Enter a positive finite number, such as 2 or 0.2.", parent=self)
                return
        if self.logical_schema is None:
            self.logical_schema = empty_logical_bit_schema(*self._built_shape)
        old_id = None if create else self.selected_group_id
        old_group = self._group_by_id(old_id)
        old_active = set(self._active_bit_ids())
        if old_id is not None:
            for key in ("alignment_groups", "logical_bits", "dark_groups"):
                self.logical_schema[key] = [
                    group
                    for group in self.logical_schema.get(key, [])
                    if str(group.get("id")) != old_id
                ]
        group_id = self._unique_group_id(label, old_id)
        group = {
            "id": group_id,
            "label": label,
            "display_color": (
                str(old_group.get("display_color"))
                if old_group is not None
                else logical_group_color(len(self._all_groups()))
            ),
            "physical_sites": [[row + 1, column + 1] for row, column in sorted(selected)],
        }
        is_bit = self.group_role_var.get() == "Digital bit"
        if is_bit:
            group["brightness_factor"] = brightness_factor
            group["evidence_sites"] = list(group["physical_sites"])
            self.logical_schema.setdefault("logical_bits", []).append(group)
        elif is_dark:
            group["mask_radius_nm"] = dark_radius
            self.logical_schema.setdefault("dark_groups", []).append(group)
        else:
            self.logical_schema.setdefault("alignment_groups", []).append(group)
        old_active.discard(str(old_id))
        if is_bit and self.group_active_var.get():
            old_active.add(group_id)
        self.logical_schema["active_logical_bits"] = sorted(old_active)
        self.selected_group_id = group_id
        self.selected_group_ids = [group_id]
        self._build_logical_controls(self.logical_schema, old_active)
        self._settings_changed()

    def _delete_group(self) -> None:
        if not self.selected_group_ids or self.logical_schema is None:
            return
        deleted = set(self.selected_group_ids)
        active = set(self._active_bit_ids())
        for key in ("alignment_groups", "logical_bits", "dark_groups"):
            self.logical_schema[key] = [
                group
                for group in self.logical_schema.get(key, [])
                if str(group.get("id")) not in deleted
            ]
        active.difference_update(deleted)
        self.logical_schema["active_logical_bits"] = sorted(active)
        self.selected_group_id = None
        self.selected_group_ids = []
        self.group_name_var.set("")
        self._build_logical_controls(self.logical_schema, active)
        self._settings_changed()

    def _toggle_group_active(self) -> None:
        if self.logical_schema is None:
            return
        for group_id in self.selected_group_ids:
            variable = self.logical_stroke_variables.get(group_id)
            if variable is not None:
                variable.set(not variable.get())
        active = self._active_bit_ids()
        self.logical_schema["active_logical_bits"] = active
        self._build_logical_controls(self.logical_schema, active)
        self._settings_changed()

    def _apply_to_rgb(self):
        self.winfo_toplevel().rgb_image_view._use_barcode_template()

    def _load_logical_schema(self) -> None:
        path_text = filedialog.askopenfilename(
            title="Load logical-bit schema",
            initialdir=str(self.initial_directory if self.initial_directory.exists() else APP_DIR),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path_text:
            return
        try:
            payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Schema JSON must contain an object.")
            raw_schema = payload.get("logical_model", payload)
            if not isinstance(raw_schema, dict):
                raise ValueError("The selected file does not contain a logical-bit schema.")
            rows = int(raw_schema.get("physical_rows"))
            columns = int(raw_schema.get("physical_columns"))
            loaded_active_ids = [str(value) for value in raw_schema.get("active_logical_bits", [])]
            schema = normalize_logical_bit_schema(raw_schema, rows, columns)
            schema["active_logical_bits"] = loaded_active_ids
            loaded_sites = set(
                logical_template_sites(rows, columns, loaded_active_ids, schema=schema)
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror("Invalid bit schema", str(exc), parent=self)
            return
        self.logical_schema = schema
        if "spacing_x_nm" in schema:
            self.spacing_x_var.set(str(schema["spacing_x_nm"]))
        if "spacing_y_nm" in schema:
            self.spacing_y_var.set(str(schema["spacing_y_nm"]))
        offsets = schema.get("column_offsets_nm", [0.0] * columns)
        self.column_gap_var.set(str(float(offsets[6]) - float(offsets[5]) if columns > 6 else 0.0))
        self.rows_var.set(str(rows))
        self.columns_var.set(str(columns))
        self.initial_directory = Path(path_text).parent
        self.source_name.set(
            "Loaded arbitrary logical-bit schema {} ({} bits).".format(
                Path(path_text).name, len(schema["logical_bits"])
            )
        )
        self._rebuild_grid(select_all=False, selected_sites=loaded_sites)

    def _save_logical_schema(self) -> None:
        if self.logical_schema is None or not self._all_groups():
            messagebox.showinfo(
                "No bit schema",
                "Create at least one group first.",
                parent=self,
            )
            return
        path_text = filedialog.asksaveasfilename(
            title="Save reusable logical-bit schema",
            initialdir=str(self.initial_directory if self.initial_directory.exists() else APP_DIR),
            initialfile="origami_logical_bits.json",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
        )
        if not path_text:
            return
        try:
            path = Path(path_text)
            self._settings()
            schema = normalize_logical_bit_schema(self.logical_schema, *self._built_shape)
            path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
            self.initial_directory = path.parent
            self.status.set(f"Saved reusable logical-bit schema: {path}")
        except OSError as exc:
            messagebox.showerror("Could not save bit schema", str(exc), parent=self)

    def load_replacement_panel(
        self,
        group: str,
        panel: Dict[str, object],
        selected_sites: Sequence[Tuple[int, int]],
    ) -> None:
        """Replace the editor state with the selected positions from one panel."""
        rows = list(panel["rows"])
        columns = list(panel["columns"])
        self.rows_var.set(str(len(rows)))
        self.columns_var.set(str(len(columns)))
        # The replacement-sheet lattice is physical and uniformly spans
        # 121.1 x 35.7 nm including the extra 10 nm column 6–7 gap.
        self.column_gap_var.set(str(EXTENSION_EXTRA_COLUMN_GAP_NM) if len(columns) > 6 else "0.0")
        self.spacing_x_var.set(str(EXTENSION_COLUMN_SPACING_NM))
        self.spacing_y_var.set(str(EXTENSION_ROW_SPACING_NM))
        self.sigma_var.set("1.5")
        self.logical_schema = empty_logical_bit_schema(len(rows), len(columns))
        self.selected_group_id = None
        self.selected_group_ids = []
        self.source_name.set(
            "Loaded {} selected site{} from {} → {} (columns {:.4g} nm; rows {:.4g} nm).".format(
                len(selected_sites),
                "" if len(selected_sites) == 1 else "s",
                group,
                panel["label"],
                EXTENSION_COLUMN_SPACING_NM,
                EXTENSION_ROW_SPACING_NM,
            )
        )
        # Panel selections arrive in displayed coordinates. Recover canonical
        # sequence-column IDs before the template editor mirrors its display.
        canonical_sites = {
            (row, len(columns) - 1 - column if panel.get("mirror_columns", False) else column)
            for row, column in selected_sites
        }
        self._rebuild_grid(selected_sites=canonical_sites)

    def _toggle_site(self, site: Tuple[int, int]) -> None:
        variable = self.site_variables[site]
        variable.set(not variable.get())
        self._paint_site(site)
        self._settings_changed()

    def _paint_site(self, site: Tuple[int, int]) -> None:
        selected = self.site_variables[site].get()
        row, column = site
        self.site_buttons[site].configure(
            text="{}S{}".format("✓ " if selected else "", row * self._built_shape[1] + column + 1),
            style="SelectedLattice.TButton" if selected else "Lattice.TButton",
        )

    def _selected_sites(self) -> List[Tuple[int, int]]:
        return [site for site, variable in self.site_variables.items() if variable.get()]

    def _rendered_sites(self) -> List[Tuple[int, int]]:
        if self.logical_schema is None or not self._all_groups():
            return []
        return logical_template_sites(
            *self._built_shape,
            self._active_bit_ids(),
            schema=self.logical_schema,
        )

    def _select_all(self) -> None:
        for site, variable in self.site_variables.items():
            variable.set(True)
            self._paint_site(site)
        self._settings_changed()

    def _clear_all(self) -> None:
        for site, variable in self.site_variables.items():
            variable.set(False)
            self._paint_site(site)
        self._settings_changed()

    def _invert(self) -> None:
        for site, variable in self.site_variables.items():
            variable.set(not variable.get())
            self._paint_site(site)
        self._settings_changed()

    def _settings_changed(self) -> None:
        group = self._group_by_id(self.selected_group_id)
        group_label = "Editing: {}".format(group.get("label", self.selected_group_id)) if group else "New group"
        if len(self.selected_group_ids) > 1:
            group_label = "{} groups selected".format(len(self.selected_group_ids))
        self.site_selection_summary.set(
            "{} • {} sites selected • ✓ Green = selected for editing".format(group_label, len(self._selected_sites()))
        )
        try:
            rows, columns, spacing_x, spacing_y, margin, _sigma, _width = self._settings()
            if (rows, columns) != self._built_shape:
                self.status.set("Grid dimensions changed — click Apply grid size before saving.")
                return
            positions = template_column_positions(columns, spacing_x, self.logical_schema)
            width_nm, height_nm = template_size_nm(rows, columns, spacing_x, spacing_y, margin)
            site_width_nm = positions[-1]
            width_nm = site_width_nm + 2 * margin
            gap_padding = max(0, int(round(float(self.column_gap_var.get()) * 3))) if columns > 6 else 0
            for (row, column), button in self.site_buttons.items():
                button.grid_configure(padx=(2 + gap_padding, 2) if column == 5 else 2)
            site_height_nm = (rows - 1) * spacing_y
            group_count = len(self._all_groups())
            active_count = len(self._active_bit_ids())
            selection_text = (
                f"{group_count} custom groups • {active_count} digital bits ON • "
                f"{len(self._selected_sites())} sites selected for editing"
            )
            self.status.set(
                "{} • site span {:.3g} × {:.3g} nm • raster with margin {:.3g} × {:.3g} nm".format(
                    selection_text, site_width_nm, site_height_nm, width_nm, height_nm
                )
            )
        except ValueError as exc:
            self.status.set(str(exc))
        self._draw_preview()

    def _draw_preview(self) -> None:
        self.preview.delete("all")
        try:
            rows, columns, spacing_x, spacing_y, margin, sigma, _width = self._settings()
        except ValueError:
            return
        positions = template_column_positions(columns, spacing_x, self.logical_schema)
        width_nm, height_nm = template_size_nm(rows, columns, spacing_x, spacing_y, margin)
        width_nm = positions[-1] + 2 * margin
        available_width = max(40, self.preview.winfo_width() - 50)
        available_height = max(40, self.preview.winfo_height() - 50)
        scale = min(available_width / width_nm, available_height / height_nm)
        draw_width, draw_height = width_nm * scale, height_nm * scale
        left = (self.preview.winfo_width() - draw_width) / 2.0
        top = (self.preview.winfo_height() - draw_height) / 2.0
        self.preview.create_rectangle(
            left, top, left + draw_width, top + draw_height, fill="#000000", outline="#666b70"
        )
        # Match the physical site-mask circles in the analysis overlay.
        radius_nm = 7.5
        radius = radius_nm * scale
        center_radius = min(radius * 0.2, max(1.0, sigma * scale * 0.5))
        selected = set(self._rendered_sites())
        group_colors: Dict[Tuple[int, int], List[str]] = defaultdict(list)
        if self.logical_schema is not None:
            active = set(self._active_bit_ids())
            groups = list(self.logical_schema.get("alignment_groups", [])) + [
                bit
                for bit in self.logical_schema.get("logical_bits", [])
                if str(bit.get("id")) in active
            ]
            for group in groups:
                color = str(group.get("display_color", "#ffffff"))
                for group_row, group_column in group.get("physical_sites", []):
                    group_colors[(int(group_row) - 1, int(group_column) - 1)].append(color)
        for group in (self.logical_schema or {}).get("dark_groups", []):
            dark_radius = float(group.get("mask_radius_nm", 5.0)) * scale
            for group_row, group_column in group["physical_sites"]:
                x = left + (width_nm - margin - positions[int(group_column) - 1]) * scale
                y = top + (margin + (int(group_row) - 1) * spacing_y) * scale
                self.preview.create_oval(x-dark_radius, y-dark_radius, x+dark_radius, y+dark_radius,
                                         fill="", outline="#38bdf8", dash=(3, 2), tags=("dark-mask",))
        for row in range(rows):
            for column in range(columns):
                x = left + (width_nm - margin - positions[column]) * scale
                y = top + (margin + row * spacing_y) * scale
                if (row, column) in selected:
                    colors = group_colors.get((row, column), ["#fff3a0"])
                    components = [
                        tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
                        for color in colors
                    ]
                    blended = "#{:02x}{:02x}{:02x}".format(
                        *(max(component[channel] for component in components) for channel in range(3))
                    )
                    self.preview.create_oval(
                        x - radius,
                        y - radius,
                        x + radius,
                        y + radius,
                        fill="",
                        outline=blended,
                        width=1.5,
                        tags=("site-mask",),
                    )
                    self.preview.create_oval(
                        x - center_radius, y - center_radius,
                        x + center_radius, y + center_radius,
                        fill=blended, outline="", tags=("site-center",),
                    )
                else:
                    self.preview.create_oval(
                        x - 3, y - 3, x + 3, y + 3, fill="", outline="#596169"
                    )
        self.preview.create_text(
            left + draw_width / 2,
            top - 8,
            text=f"Site circles r = {radius_nm:g} nm · column pitch {spacing_x:g} nm · row pitch {spacing_y:g} nm · extra C6–C7 gap {float(self.column_gap_var.get()):g} nm",
            fill="#aeb5ba", anchor="s",
        )
        self.preview.create_text(
            left + 8,
            top + draw_height - 8,
            text="site span {:.3g} × {:.3g} nm; raster {:.3g} × {:.3g} nm".format(
                positions[-1],
                (rows - 1) * spacing_y,
                width_nm,
                height_nm,
            ),
            fill="#aeb5ba",
            anchor="sw",
        )

    def _save(self) -> None:
        try:
            settings = self._settings()
            if settings[:2] != self._built_shape:
                raise ValueError("Click Apply grid size after changing the row or column count.")
            if self.logical_schema is None or not self._all_groups():
                raise ValueError("Create at least one group before saving a template.")
            selected = self._rendered_sites()
            if not selected:
                raise ValueError("Turn on a digital bit or create an alignment-only group before saving.")
        except ValueError as exc:
            messagebox.showerror("Cannot save template", str(exc), parent=self)
            return
        initial_directory = self.initial_directory if self.initial_directory.exists() else APP_DIR
        path = filedialog.asksaveasfilename(
            title="Save origami barcode template",
            initialdir=str(initial_directory),
            initialfile="origami_barcode_template.png",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
        )
        if not path:
            return
        try:
            png_path, metadata_path, metadata = save_barcode_template(
                Path(path),
                settings[0],
                settings[1],
                selected,
                settings[2],
                settings[3],
                settings[4],
                settings[5],
                settings[6],
                self._active_bit_ids(),
                self.logical_schema,
            )
            self.initial_directory = png_path.parent
            self.status.set(
                "Saved {}-site template: {} (metadata: {})".format(
                    metadata["selected_site_count"], png_path, metadata_path.name
                )
            )
            messagebox.showinfo(
                "Template saved",
                "Upload this image as the custom origami template:\n{}\n\n"
                "Its nm-per-pixel calibration is embedded in the PNG and also saved here:\n{}".format(
                    png_path, metadata_path
                ),
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("Could not save template", str(exc), parent=self)


class PicklistApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        config_defaults = load_json(CONFIG_EXAMPLE_PATH, DEFAULT_CONFIG)
        plate_defaults = load_json(PLATE_STATE_EXAMPLE_PATH, DEFAULT_PLATE_STATE)
        storage_defaults = load_json(STORAGE_STATE_EXAMPLE_PATH, DEFAULT_STORAGE_STATE)
        self.config = load_json(CONFIG_PATH, config_defaults)
        # Apply the requested default once to existing installations as well.
        # An older running app may have saved its former 75 nL setting again.
        if not self.config.get("base_transfer_25nl_migrated", False):
            self.config["transfer_volume_nL"] = 25
            self.config["base_transfer_25nl_migrated"] = True
            save_json(CONFIG_PATH, self.config)
        self.plate_state = load_json(PLATE_STATE_PATH, plate_defaults)
        self.storage_state = load_json(STORAGE_STATE_PATH, storage_defaults)
        if not CONFIG_PATH.exists():
            save_json(CONFIG_PATH, self.config)
        if not PLATE_STATE_PATH.exists():
            save_json(PLATE_STATE_PATH, self.plate_state)
        if not STORAGE_STATE_PATH.exists():
            save_json(STORAGE_STATE_PATH, self.storage_state)
        self._config_save_job = None
        self.title(APP_TITLE)
        self.geometry("1280x880")
        self.minsize(960, 700)
        self._set_style()
        self.set_views: List[SetView] = []
        self.last_picklist: List[Dict[str, object]] = []
        self.last_mix: List[Dict[str, float]] = []
        self.last_design_names: List[str] = []
        self.last_selection_panels: List[Dict[str, object]] = []
        self.last_run_id = ""
        self.last_run_committed = False
        self.last_run_saved = False
        self.last_run_stored = False
        self._advancing_destination = False
        self._build()

    def _set_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Generate.TButton", font=("TkDefaultFont", 11, "bold"), padding=8)
        style.configure("MakePicklist.TButton", font=("TkDefaultFont", 16, "bold"),
                        padding=(24, 14), background="#218739", foreground="#ffffff")
        style.map("MakePicklist.TButton", background=[("pressed", "#146329"), ("active", "#2a9b46")],
                  foreground=[("active", "#ffffff")])
        style.configure("Compact.MakePicklist.TButton", font=("TkDefaultFont", 12, "bold"),
                        padding=(16, 5))
        style.layout("Grouped.TNotebook.Tab", [])
        style.configure("Navigation.TButton", padding=(8, 4))
        style.map("Navigation.TButton", background=[("selected", "#c5dfd0")])

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(14, 5))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Select replacements, preview the run, then save it or send it to storage.").pack(side="left", padx=18)
        navigation = ttk.Frame(self)
        navigation.pack(fill="x", padx=12, pady=(0, 4))
        navigation.columnconfigure(1, weight=1)
        navigation.columnconfigure(3, weight=1)
        main_tabs = ttk.Notebook(self, style="Grouped.TNotebook")
        main_tabs.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        replacement_page = ttk.Frame(main_tabs)
        template_page = ttk.Frame(main_tabs)
        settings_page = ttk.Frame(main_tabs)
        plate_page = ttk.Frame(main_tabs)
        storage_page = ttk.Frame(main_tabs)
        preview_page = ttk.Frame(main_tabs)
        main_tabs.add(replacement_page, text="1. Replacements")
        main_tabs.add(settings_page, text="2. Run & Mixing Settings", state="disabled")
        main_tabs.add(preview_page, text="3. Results", state="disabled")
        main_tabs.add(plate_page, text="4. Destination Plate")
        main_tabs.add(storage_page, text="5. Storage")
        self.main_tabs = main_tabs
        self.replacement_page = replacement_page
        self.settings_page = settings_page
        self.preview_page = preview_page
        self.template_page = template_page

        configured_output = Path(str(self.config.get("output_root_path", OUTPUT_DIR))).expanduser()
        if not configured_output.is_absolute():
            configured_output = APP_DIR / configured_output
        self.template_view = OrigamiTemplateView(template_page, configured_output)
        self.template_view.pack(fill="both", expand=True)

        self.make_picklist_button = ttk.Button(
            replacement_page, text="Make Picklist", style="Compact.MakePicklist.TButton",
            command=self._make_picklist,
        )
        self.make_picklist_button.pack(side="bottom", fill="x", padx=12, pady=(2, 4))
        self._build_compatibility_footer(replacement_page)
        replacement_scroll = ScrollFrame(replacement_page)
        replacement_scroll.pack(side="top", fill="both", expand=True)
        replacement_content = replacement_scroll.inner
        hinge_controls = ttk.Frame(replacement_content, padding=(12, 3))
        hinge_controls.pack(side="top", fill="x")
        self.hinge_type = tk.StringVar(
            value=HINGE_TYPES.get(str(self.config.get("hinge_type", "original")), HINGE_TYPES["original"])
        )
        ttk.Label(hinge_controls, text="Hinge type").pack(side="left", padx=(0, 8))
        ttk.Combobox(
            hinge_controls, textvariable=self.hinge_type,
            values=list(HINGE_TYPES.values()), state="readonly", width=38,
        ).pack(side="left")
        self.base_plate_options = BASE_PLATES
        saved_base_plate = self.config.get("base_plate", next(iter(BASE_PLATES)))
        # The user's physical 20 µM base stock now uses the interleaved layout.
        # Preserve explicitly chosen legacy layouts, but migrate the old generic label.
        if saved_base_plate == "LowConc base plate — 20 µM":
            saved_base_plate = "LowConc base plate — 20 µM (interleaved)"
        self.base_plate = tk.StringVar(value=saved_base_plate if saved_base_plate in BASE_PLATES else next(iter(BASE_PLATES)))
        if BASE_PLATES[self.base_plate.get()][0] == "book_base_20uM_interleaved.csv":
            self.hinge_type.set(HINGE_TYPES["balanced_rigid"])
        self.transfer_volume = tk.StringVar(value=str(self.config.get("transfer_volume_nL", 25)))
        set_tabs = ttk.Notebook(replacement_content)
        set_tabs.pack(side="top", fill="x")
        for title, filename, plate_name, panels in panel_definitions():
            if title not in ACTIVE_REPLACEMENT_TABS:
                continue
            page = ttk.Frame(set_tabs)
            set_tabs.add(page, text=title)
            self.set_views.append(
                (SmartMiRNASetView if title == "miRNA" else SetView)(
                    page,
                    title,
                    SHEET_DIR / filename,
                    plate_name,
                    panels,
                    selection_changed=self._selections_changed,
                    template_requested=self._make_template_from_panel,
                    base_transfer_volume=self.transfer_volume,
                    scrollable=False,
                )
            )

        for view in self.set_views:
            view.path_var.trace_add("write", lambda *_args: self._selections_changed())
        self._build_settings(settings_page, main_tabs, preview_page)
        self.base_plate.trace_add("write", self._base_plate_changed)
        self._build_manual_synthesis(replacement_content, set_tabs)
        plate_actions = ttk.Frame(plate_page, padding=(12, 10, 12, 0))
        plate_actions.pack(fill="x")
        ttk.Button(plate_actions, text="Start fresh plate", command=self._start_fresh_plate).pack(side="left")
        ttk.Label(
            plate_actions,
            text="Clears recorded usage for the currently named destination plate; generated files are not deleted.",
        ).pack(side="left", padx=12)
        self.plate_view = DestinationPlateView(plate_page, self._save_destination_well)
        self.plate_view.pack(fill="both", expand=True)
        self._build_storage(storage_page)
        self._build_preview(preview_page)
        image_page = ttk.Frame(main_tabs)
        self.rgb_image_page = image_page
        main_tabs.add(image_page, text="6. RGB Image")
        main_tabs.add(template_page, text="7. Origami Templates")
        image_scroll = ScrollFrame(image_page)
        image_scroll.pack(fill="both", expand=True)
        self.rgb_image_view = RGBImageView(image_scroll.inner, self, APP_DIR, STORAGE_STATE_PATH, PLATE_STATE_PATH)
        self.rgb_image_view.pack(fill="both", expand=True)
        from barcode_guard_view import BarcodeGuard
        self.barcode_guard = BarcodeGuard(replacement_content, self, STORAGE_STATE_PATH.with_name("barcode_registry.json"))
        self.barcode_guard.pack(side="top", fill="x", padx=12, after=set_tabs)
        barcode_pool = ttk.Frame(self.rgb_image_view)
        barcode_pool.pack(fill="x", before=self.rgb_image_view.winfo_children()[0])
        ttk.Label(barcode_pool, text="Barcode experiment / pool (shared with Replacements)").pack(side="left")
        ttk.Entry(barcode_pool, textvariable=self.barcode_guard.experiment, width=24).pack(side="left", padx=6)
        ttk.Button(barcode_pool, text="Barcode registry…", command=self.barcode_guard.show_registry).pack(side="left")
        self.navigation_buttons = {}
        groups = (
            (replacement_page, settings_page, preview_page),
            (plate_page, storage_page),
            (image_page, template_page),
        )
        for index, pages in enumerate(groups):
            group = ttk.Frame(navigation)
            group.grid(row=0, column=index * 2, sticky="w" if index == 0 else "e")
            for page in pages:
                button = ttk.Button(
                    group, text=main_tabs.tab(page, "text"), style="Navigation.TButton",
                    command=lambda target=page: main_tabs.select(target),
                )
                button.pack(side="left", padx=1)
                self.navigation_buttons[str(page)] = button
        main_tabs.bind("<<NotebookTabChanged>>", self._sync_navigation, add="+")
        self._sync_navigation()
        self._bind_config_updates()
        self._bind_preview_updates()
        self._refresh_plate_view()
        self.protocol("WM_DELETE_WINDOW", self._close_app)

    def _sync_navigation(self, *_args) -> None:
        selected = self.main_tabs.select()
        for page, button in getattr(self, "navigation_buttons", {}).items():
            button.state([
                "disabled" if self.main_tabs.tab(page, "state") == "disabled" else "!disabled",
                "selected" if page == selected else "!selected",
            ])

    def _make_template_from_panel(
        self,
        group: str,
        panel: Dict[str, object],
        selected_sites: Sequence[Tuple[int, int]],
    ) -> None:
        self.template_view.load_replacement_panel(group, panel, selected_sites)
        self.main_tabs.select(self.template_page)

    def _build_compatibility_footer(self, page: ttk.Frame) -> None:
        footer = ttk.LabelFrame(page, text="Selection compatibility", padding=3)
        footer.pack(side="bottom", fill="x", padx=6, pady=(1, 2))
        top = ttk.Frame(footer)
        top.pack(fill="x")
        ttk.Button(top, text="Check selections", command=self.check_selections).pack(side="left")
        ttk.Button(top, text="Clear all selections", command=self.clear_all_selections).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(top, text="Save selections PDF", command=self.save_selections_pdf).pack(
            side="left", padx=(8, 0)
        )
        self.compatibility_status = tk.Label(
            top,
            text="Not checked",
            foreground="#555555",
            background="#f0f0f0",
        )
        self.compatibility_status.pack(side="left", padx=12)
        details = ttk.Frame(footer)
        details.pack(fill="x", pady=(2, 0))
        self.clash_text = tk.Text(
            details,
            height=2,
            wrap="word",
            relief="solid",
            borderwidth=1,
            background="#ffffff",
            foreground="#333333",
        )
        scrollbar = ttk.Scrollbar(details, orient="vertical", command=self.clash_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.clash_text.configure(yscrollcommand=scrollbar.set)
        self.clash_text.pack(side="left", fill="x", expand=True)
        self._set_clash_text("Select replacement wells, then click Check selections.")

    def clear_all_selections(self) -> None:
        """Restore every replacement set and compatibility indicator to its initial state."""
        for view in self.set_views:
            view.clear()
        self.compatibility_status.configure(text="Not checked", foreground="#555555")
        self._set_clash_text("Select replacement wells, then click Check selections.")

    def _replacement_selection_panels(
        self, selected_names: Optional[Set[str]] = None
    ) -> List[Dict[str, object]]:
        """Describe selected positions across every replacement panel."""
        panels = []
        for view in self.set_views:
            names_for_view = set(view.selected()) if selected_names is None else selected_names
            resolve_name = getattr(view, "resolved_name", lambda name: name)
            if not names_for_view:
                continue
            for panel in view.panels:
                rows = [str(value) for value in panel["rows"]]
                columns = [str(value) for value in panel_display_columns(panel)]
                configured = panel.get("active")
                active = {
                    (row, column)
                    for row in rows
                    for column in columns
                    if configured is None or (row, column) in configured
                }
                selected = []
                names = []
                for row in rows:
                    for column in columns:
                        raw_name = panel_sequence_name(panel, row, column)
                        name = raw_name if raw_name in names_for_view else resolve_name(raw_name)
                        variant = getattr(view, "smart_variants", {}).get(raw_name, {})
                        if name not in names_for_view and variant.get("name") in names_for_view:
                            name = variant["name"]
                        if name in names_for_view:
                            selected.append((row, column))
                            names.append(name)
                if selected:
                    panels.append(
                        {
                            "group": view.title,
                            "x_shift": list(SHIFT_STATES).index(view.x_shift.get()) if view.title in COLOR_BITS else None,
                            "y_shift": list(SHIFT_STATES).index(view.y_shift.get()) if view.title in COLOR_BITS else None,
                            "label": str(panel["label"]),
                            "rows": rows,
                            "columns": columns,
                            "active": sorted(active),
                            "selected": selected,
                            "selected_names": names,
                            "site_fractions": {name: view.site_fractions.get(name, 1.0) for name in names},
                            "site_concentrations_nm": {name: view.site_target(name) for name in names},
                            "spacing_x_nm": panel.get("spacing_x_nm"),
                            "spacing_y_nm": panel.get("spacing_y_nm"),
                            "mirror_columns": bool(panel.get("mirror_columns", False)),
                        }
                    )
        return panels

    def _shared_export_directory(self) -> Path:
        directory = Path(self.output_root.get()).expanduser()
        if not directory.is_absolute():
            directory = APP_DIR / directory
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save_selections_pdf(self) -> None:
        """Export selected replacements from every panel as one PDF report."""
        panels = self._replacement_selection_panels()
        if not panels:
            messagebox.showinfo(
                "No replacements selected",
                "Select at least one replacement before saving a PDF.",
                parent=self,
            )
            return

        try:
            run_directory = self._shared_export_directory()
            path = run_directory / "replacement_selections.pdf"
            write_replacement_selection_pdf(path, panels)
            messagebox.showinfo(
                "Selections PDF saved",
                "Saved a PDF of {} selected replacement{} to:\n{}".format(
                    sum(len(panel["selected_names"]) for panel in panels),
                    "" if sum(len(panel["selected_names"]) for panel in panels) == 1 else "s",
                    path,
                ),
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("Could not save selections PDF", str(exc), parent=self)

    def _set_clash_text(self, text: str) -> None:
        self.clash_text.configure(state="normal")
        self.clash_text.delete("1.0", "end")
        self.clash_text.insert("1.0", text)
        self.clash_text.configure(state="disabled")

    def _selections_changed(self) -> None:
        self._resolve_smart_common_locks()
        self._invalidate_preview()
        if hasattr(self, "barcode_guard"):
            self.barcode_guard.refresh()
        self._lock_run_settings()
        for view in self.set_views:
            view.mark_conflicts(set())
        self.compatibility_status.configure(text="Selections changed — check again", foreground="#8a5a00")
        self._set_clash_text("Compatibility results are out of date. Click Check selections to run the check again.")

    def _lock_run_settings(self) -> None:
        if self.main_tabs.select() == str(self.settings_page):
            self.main_tabs.select(self.replacement_page)
        self.main_tabs.tab(self.settings_page, state="disabled")
        self._sync_navigation()

    def _make_picklist(self) -> None:
        """Open run settings only after selected replacements pass the clash check."""
        self._lock_run_settings()
        if not self.check_selections():
            return
        if not any(view.selected() for view in self.set_views):
            self.compatibility_status.configure(text="No replacements selected", foreground="#a40000")
            self._set_clash_text("Select at least one replacement, then click Make Picklist.")
            return
        try:
            self.barcode_guard.check_manual()
        except ValueError as exc:
            messagebox.showerror("Barcode check failed", str(exc), parent=self)
            return
        self.main_tabs.tab(self.settings_page, state="normal")
        self.main_tabs.select(self.settings_page)
        self._sync_navigation()

    def _resolve_smart_common_locks(self):
        """Replace an explicitly chosen lock/R1 overlap with one combined source."""
        smart_views = [view for view in self.set_views if isinstance(view, SmartMiRNASetView)]
        r1_by_target = defaultdict(list)
        for view in self.set_views:
            if view.title not in {"PAINT R1", "LowConc PAINT R1"}:
                continue
            selected = {name for name in view.raw_selected() if name.endswith("_R1")}
            if not selected:
                continue
            try:
                records, _ = parse_source2(Path(view.path_var.get()))
            except (OSError, ValueError):
                continue  # The normal compatibility check reports unreadable source sheets.
            for record in records:
                if record["Name"] in selected:
                    r1_by_target[record["Replace Well"].upper()].append((view, record["Name"]))
        absorbed = {view: set() for view in self.set_views}
        changed = False
        for locks in smart_views:
            combined = set()
            for plain, variant in locks.smart_variants.items():
                overlaps = r1_by_target.get(variant["replace_well"].upper(), [])
                if plain not in locks.variables or not locks.variables[plain].get() or not overlaps or variant["name"] not in locks.available:
                    continue
                combined.add(plain)
                for view, name in overlaps:
                    absorbed[view].add(name)
            changed |= locks.smart_combined != combined
            locks.smart_combined = combined
            locks._sync_smart_metadata()
            locks._update_smart_status()
            locks._update_selection_summary()
        for view, names in absorbed.items():
            repaint = view.smart_absorbed.symmetric_difference(names)
            changed |= bool(repaint)
            view.smart_absorbed = names
            for name in repaint:
                view._paint_square(name)
            view._update_selection_summary()
        if changed:
            self._invalidate_preview()

    def check_selections(self) -> bool:
        """Highlight selections that replace the same base well and list every clash."""
        targets = defaultdict(list)
        conflicts_by_view = {view: set() for view in self.set_views}
        try:
            self._resolve_smart_common_locks()
            for view in self.set_views:
                selected_names = set(view.selected())
                if not selected_names:
                    continue
                rows = [row for path, _plate, _stock, _names in SetView.replacement_sources(view, selected_names)
                        for row in parse_source2(path)[0]]
                missing = selected_names - {row["Name"] for row in rows}
                if missing:
                    raise ValueError("{}: selected replacements are missing from the CSV: {}".format(
                        view.title, ", ".join(sorted(missing))))
                for row in rows:
                    name = row["Name"]
                    if name in selected_names:
                        targets[row["Replace Well"].upper()].append((view, name))

            clashes = []
            for target, entries in sorted(targets.items()):
                unique_entries = []
                seen = set()
                for view, name in entries:
                    key = (view.title, name)
                    if key not in seen:
                        seen.add(key)
                        unique_entries.append((view, name))
                if len(unique_entries) < 2:
                    continue
                clashes.append((target, unique_entries))
                for view, name in unique_entries:
                    conflicts_by_view[view].add(name)

            for view, names in conflicts_by_view.items():
                view.mark_conflicts(names)

            if clashes:
                lines = []
                for target, entries in clashes:
                    labels = ["{} — {}".format(view.title, name) for view, name in entries]
                    lines.append("{}: {}".format(target, "  ↔  ".join(labels)))
                self.compatibility_status.configure(
                    text="{} clash{} found".format(len(clashes), "" if len(clashes) == 1 else "es"),
                    foreground="#a40000",
                )
                self._set_clash_text("\n".join(lines))
                return False

            self.compatibility_status.configure(text="Compatible — no clashes", foreground="#18733b")
            self._set_clash_text("No selected replacements target the same base well.")
            return True
        except Exception as exc:
            for view in self.set_views:
                view.mark_conflicts(set())
            self.compatibility_status.configure(text="Compatibility check failed", foreground="#a40000")
            self._set_clash_text(str(exc))
            return False

    def _field(self, parent: tk.Widget, row: int, label: str, value: str, width: int = 58) -> tk.StringVar:
        variable = tk.StringVar(value=value)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=6, pady=4)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        return variable

    def _restore_path(self, key: str, fallback: Path) -> Path:
        value = self.config.get(key)
        if not value:
            return Path(fallback)
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else APP_DIR / path

    @staticmethod
    def _portable_path(value: str) -> str:
        path = Path(value).expanduser()
        try:
            return str(path.resolve().relative_to(APP_DIR))
        except ValueError:
            return str(path)

    def base_stock_concentration_nm(self):
        return BASE_PLATES[self.base_plate.get()][1]

    def _build_manual_synthesis(self, parent, before):
        self._syncing_manual = False
        saved = self.config.get("manual_transfer_limits", {})
        self.manual_minimum_transfer = tk.StringVar(value=str(saved.get("minimum_transfer_nl",25)))
        self.manual_transfer_increment = tk.StringVar(value=str(saved.get("transfer_increment_nl",25)))
        parts = self.desired.get().split(",")
        self.manual_final_volume = tk.StringVar(value=parts[0] if len(parts)==6 else "100")
        self.manual_base_target = tk.StringVar(value=parts[1] if len(parts)==6 else "50")
        self.manual_scaffold_target = tk.StringVar(value=parts[2] if len(parts)==6 else "10")
        self.manual_synthesis_status = tk.StringVar()
        self.manual_stock_label = tk.StringVar()
        frame = ttk.LabelFrame(parent,text="Manual synthesis per run",padding=3)
        frame.pack(side="top",fill="x",padx=12,before=before)
        ttk.Label(frame,text="Book-base plate").grid(row=0,column=0,sticky="w")
        ttk.Combobox(frame,textvariable=self.base_plate,values=tuple(BASE_PLATES),state="readonly",width=43).grid(row=0,column=1,columnspan=3,sticky="w",padx=5)
        ttk.Label(frame,textvariable=self.manual_stock_label).grid(row=0,column=4,columnspan=4,sticky="w")
        fields=(("Final volume (µL)",self.manual_final_volume),("Book-base target (nM)",self.manual_base_target),
                ("Scaffold target (nM)",self.manual_scaffold_target),("Minimum transfer (nL)",self.manual_minimum_transfer),
                ("Transfer increment (nL)",self.manual_transfer_increment))
        fields_row = ttk.Frame(frame)
        fields_row.grid(row=1,column=0,columnspan=8,sticky="ew",pady=(1,0))
        for index,(label,variable) in enumerate(fields):
            col=index*2
            ttk.Label(fields_row,text=label).grid(row=0,column=col,sticky="w",padx=(4,0))
            ttk.Entry(fields_row,textvariable=variable,width=9).grid(row=0,column=col+1,sticky="w",padx=(4,16))
        status = ttk.Label(frame,textvariable=self.manual_synthesis_status,foreground="#a40000",wraplength=1000)
        def show_synthesis_error(*_args):
            if self.manual_synthesis_status.get():
                status.grid(row=2,column=0,columnspan=8,sticky="w",pady=(4,0))
            else:
                status.grid_remove()
        self.manual_synthesis_status.trace_add("write",show_synthesis_error)
        for variable in (self.manual_final_volume,self.manual_base_target,self.manual_scaffold_target):
            variable.trace_add("write",self._manual_synthesis_changed)
        for variable in (self.desired,self.staple,self.manual_minimum_transfer,self.manual_transfer_increment):
            variable.trace_add("write",self._update_manual_concentrations)
        self._update_manual_concentrations()

    def _manual_transfer_limits(self):
        return dict(minimum_transfer_nl=float(self.manual_minimum_transfer.get()),
                    transfer_increment_nl=float(self.manual_transfer_increment.get()))

    def _manual_base_transfer(self):
        desired=parse_vector(self.desired.get(),6,"Desired")
        stock=parse_vector(self.staple.get(),4,"Base staple")[0]
        return synthesis_base_volume_nl(desired[0],desired[1],stock,**self._manual_transfer_limits())

    def _manual_synthesis_changed(self,*_args):
        if self._syncing_manual:return
        parts=self.desired.get().split(",")
        if len(parts)!=6:
            self.manual_synthesis_status.set("Correct the Desired mixing vector in Run & Mixing Settings first.")
            return
        self.desired.set(",".join([self.manual_final_volume.get(),self.manual_base_target.get(),self.manual_scaffold_target.get(),*parts[3:]]))

    def _update_manual_concentrations(self,*_args):
        if self._syncing_manual:return
        self._syncing_manual=True
        try:
            parts=self.desired.get().split(",")
            if len(parts)==6:
                for variable,value in zip((self.manual_final_volume,self.manual_base_target,self.manual_scaffold_target),parts):
                    if variable.get()!=value:variable.set(value)
            try:
                stock=parse_vector(self.staple.get(),4,"Base staple")[0]
                target=parse_vector(self.desired.get(),6,"Desired")[1]
                limits=self._manual_transfer_limits()
                self.manual_stock_label.set("Base stock: {:g} µM".format(stock/1000))
                volume=self._manual_base_transfer()
                self.transfer_volume.set("{:g}".format(volume))
                self.manual_synthesis_status.set("")
            except ValueError as exc:
                self.transfer_volume.set("")
                self.manual_synthesis_status.set(str(exc))
            for view in self.set_views:
                try:
                    view.base_concentration_nm=parse_vector(self.staple.get(),4,"Base staple")[0]
                    view.base_desired_concentration_nm=parse_vector(self.desired.get(),6,"Desired")[1]
                    view.minimum_transfer_nl=float(self.manual_minimum_transfer.get())
                    view.transfer_increment_nl=float(self.manual_transfer_increment.get())
                except ValueError:
                    view.base_desired_concentration_nm=float('nan')
                view.update_percentage_options(self.transfer_volume.get())
            self.config["manual_desired_mix"]=self.desired.get()
            self.config["manual_transfer_limits"]={"minimum_transfer_nl":self.manual_minimum_transfer.get(),"transfer_increment_nl":self.manual_transfer_increment.get()}
            self._schedule_config_save()
            if hasattr(self,"pick_tree"):self._invalidate_preview()
        finally:self._syncing_manual=False

    def _base_plate_changed(self, *_args):
        filename, stock = BASE_PLATES[self.base_plate.get()]
        self.base_path.set(str(SHEET_DIR / filename))
        if filename == "book_base_20uM_interleaved.csv":
            self.hinge_type.set(HINGE_TYPES["balanced_rigid"])
        parts = self.staple.get().split(",")
        self.staple.set(",".join([str(int(stock)), *(parts[1:] if len(parts) == 4 else ["1", "0", "0"])]))
        self._schedule_config_save()

    def _bind_config_updates(self) -> None:
        variables = (
            self.picklist_path,
            self.mix_path,
            self.output_root,
            self.destination_wells,
            self.destination_plate,
            self.source_plate_type,
            self.transfers_per_source,
            self.transfer_volume,
            self.max_volume,
            self.hinge_type, self.staple, self.scaffold, self.te_10x, self.magnesium, self.sodium, self.desired,
        )
        for variable in variables:
            variable.trace_add("write", lambda *_args: self._schedule_config_save())

    def _bind_preview_updates(self) -> None:
        variables = (
            self.base_path, self.destination_wells, self.destination_plate,
            self.source_plate_type, self.transfers_per_source, self.transfer_volume,
            self.max_volume, self.hinge_type, self.staple, self.scaffold,
            self.magnesium, self.sodium, self.te_10x, self.desired,
            *(view.desired_concentration_nm for view in self.set_views),
        )
        for variable in variables:
            variable.trace_add("write", self._invalidate_preview)

    def _invalidate_preview(self, *_args) -> None:
        if self._advancing_destination or not (self.last_picklist or self.last_mix):
            return
        self.last_picklist = []
        self.last_mix = []
        self._fill_tree(self.pick_tree, [], PICKLIST_COLUMNS)
        self._fill_tree(self.mix_tree, [], ("Reagent", "Volume_uL"))
        self.summary.set("Inputs changed — generate a new preview.")
        self.status.set("Inputs changed — generate a new preview before saving or storing.")
        self._update_result_action_buttons()

    def _schedule_config_save(self) -> None:
        if self._config_save_job is not None:
            self.after_cancel(self._config_save_job)
        self._config_save_job = self.after(300, self._flush_config)

    def _flush_config(self) -> None:
        self._config_save_job = None
        self.config.update(
            {
                "version": 1,
                "base_plate": self.base_plate.get(),
                "save_picklist_path": self._portable_path(self.picklist_path.get()),
                "save_recipe_path": self._portable_path(self.mix_path.get()),
                "output_root_path": self._portable_path(self.output_root.get()),
                "pending_destination_wells": normalize_recent_wells(self.destination_wells.get().split(",")),
                "destination_plate_name": self.destination_plate.get().strip() or "Destination[1]",
                "source_plate_type": self.source_plate_type.get().strip(),
                "hinge_type": self._selected_hinge_type(),
            }
        )
        for key, variable, converter in (
            ("transfers_per_source", self.transfers_per_source, int),
            ("transfer_volume_nL", self.transfer_volume, int),
            ("max_destination_volume_uL", self.max_volume, float),
        ):
            try:
                self.config[key] = converter(variable.get())
            except ValueError:
                pass
        self.config["manual_mixing_settings"] = {key:getattr(self,key).get() for key in ("staple","scaffold","te_10x","magnesium","sodium")}
        save_json(CONFIG_PATH, self.config)
        self._refresh_plate_view()

    def _refresh_plate_view(self) -> None:
        if not hasattr(self, "plate_view"):
            return
        try:
            capacity = float(self.max_volume.get())
        except ValueError:
            capacity = 12.5
        entered = normalize_recent_wells(self.destination_wells.get().split(","))
        plate_name = self.destination_plate.get().strip() or "Destination[1]"
        self.plate_view.render(plate_name, self.plate_state, entered, capacity)

    def _save_destination_well(self, plate_name: str, well: str, label: str, used: bool) -> None:
        updated = deepcopy(self.plate_state)
        edit_destination_well(updated, plate_name, well, label, used)
        try:
            save_json(PLATE_STATE_PATH, updated)
        except OSError as exc:
            messagebox.showerror("Could not save well", str(exc), parent=self)
            return
        self.plate_state = updated
        self._refresh_plate_view()
        self.status.set("Saved {} on {} as {}".format(well, plate_name, "used" if used else "unused"))

    def _destination_well_count(self) -> int:
        current = normalize_recent_wells(self.destination_wells.get().split(","))
        if current:
            return len(current)
        recent = normalize_recent_wells(self.config.get("recent_destination_wells", []))
        pending = normalize_recent_wells(self.config.get("pending_destination_wells", []))
        return len(recent) or len(pending) or 3

    def _apply_starting_well(self) -> None:
        try:
            plate_name = self.destination_plate.get().strip() or "Destination[1]"
            wells = unused_wells_from(
                self.plate_state,
                plate_name,
                self.starting_well.get(),
                self._destination_well_count(),
            )
            self.destination_wells.set(",".join(wells))
            self.status.set("Destination allocation will start at {}".format(wells[0]))
            self._refresh_plate_view()
        except ValueError as exc:
            messagebox.showerror("Invalid starting well", str(exc), parent=self)

    def _start_fresh_plate(self) -> None:
        plate_name = self.destination_plate.get().strip() or "Destination[1]"
        confirmed = messagebox.askyesno(
            "Start fresh destination plate",
            "Clear all recorded well usage and release existing stored-run well reservations for '{}'?\n\nStored runs and generated output folders will not be deleted.".format(plate_name),
            icon="warning",
            parent=self,
        )
        if not confirmed:
            return
        count = self._destination_well_count()
        updated = deepcopy(self.plate_state)
        clear_plate(updated, plate_name, self._storage_items())
        try:
            save_json(PLATE_STATE_PATH, updated)
        except OSError as exc:
            messagebox.showerror("Could not start fresh plate", str(exc), parent=self)
            return
        self.plate_state = updated
        rgb = getattr(self, "rgb_image_view", None)
        if rgb is not None and rgb.destination_plate.get().strip() == plate_name:
            rgb._invalidate()
        last_run = self.config.get("last_successful_generation")
        if isinstance(last_run, dict) and last_run.get("destination_plate_name") == plate_name:
            self.config.pop("last_successful_generation", None)
        self.config["recent_destination_wells"] = []
        self.starting_well.set("")
        self.destination_wells.set(",".join(next_unused_wells(self.plate_state, plate_name, count)))
        self._flush_config()
        self.plate_view.detail.set("{} is now recorded as a fresh, unused plate.".format(plate_name))
        self.status.set("Fresh destination plate started")

    def _close_app(self) -> None:
        if self._config_save_job is not None:
            self.after_cancel(self._config_save_job)
            self._config_save_job = None
        self._flush_config()
        self.destroy()

    def _build_settings(self, page: ttk.Frame, tabs: ttk.Notebook, preview_page: ttk.Frame) -> None:
        self.generate_preview_button = ttk.Button(
            page, text="Generate Preview", style="MakePicklist.TButton",
            command=lambda: self.generate(tabs, preview_page),
        )
        self.generate_preview_button.pack(side="bottom", fill="x", padx=12, pady=(6, 12))
        scroll = ScrollFrame(page)
        scroll.pack(fill="both", expand=True)
        page = scroll.inner
        page.columnconfigure(0, weight=1)
        pick = ttk.LabelFrame(page, text="Picklist", padding=10)
        pick.grid(row=0, column=0, sticky="new", padx=14, pady=10)
        pick.columnconfigure(1, weight=1)
        configured_plate = str(self.config.get("destination_plate_name", "Destination[1]"))
        recent = normalize_recent_wells(self.config.get("recent_destination_wells", []))
        pending = normalize_recent_wells(self.config.get("pending_destination_wells", ["A01", "A02", "A03"]))
        default_wells = next_unused_wells(self.plate_state, configured_plate, len(recent) or len(pending) or 3)
        self.base_path = self._field(pick, 0, "Base Source1", str(SHEET_DIR / BASE_PLATES[self.base_plate.get()][0]))
        self.destination_wells = self._field(pick, 1, "Destination wells", ",".join(default_wells))
        self.transfers_per_source = tk.StringVar(value="1")
        ttk.Label(pick, text="One synthesis per prepared run; all collected staples enter its mixing recipe.").grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(pick, text="Calculated base transfer volume (nL)").grid(row=3, column=0, sticky="e", padx=6, pady=4)
        ttk.Entry(pick, textvariable=self.transfer_volume, state="readonly", width=58).grid(
            row=3, column=1, sticky="ew", padx=6, pady=4)
        self.max_volume = self._field(pick, 4, "Max destination volume (µL)", str(self.config.get("max_destination_volume_uL", 12.5)))
        self.destination_plate = self._field(pick, 5, "Destination plate", configured_plate)
        self.source_plate_type = self._field(pick, 6, "Source plate type", str(self.config.get("source_plate_type", "384PP_AQ_BP")))
        self.output_root = self._field(pick, 7, "Generated output root", str(self._restore_path("output_root_path", OUTPUT_DIR)))
        self.picklist_path = self._field(pick, 8, "Latest picklist path", str(self._restore_path("save_picklist_path", OUTPUT_DIR / "picklist_combined.csv")))
        self.starting_well = self._field(pick, 9, "Destination starting well", "")
        ttk.Button(pick, text="Start here", command=self._apply_starting_well).grid(row=9, column=2, sticky="w", padx=6, pady=4)
        ttk.Label(pick, text="Base plate stock").grid(row=10, column=0, sticky="e", padx=6, pady=4)
        self.base_plate_picker = ttk.Combobox(pick, textvariable=self.base_plate,
            values=tuple(BASE_PLATES), state="readonly", width=45)
        self.base_plate_picker.grid(row=10, column=1, sticky="ew", padx=6, pady=4)
        mix = ttk.LabelFrame(page, text="Mixing recipe", padding=10)
        mix.grid(row=1, column=0, sticky="new", padx=14, pady=6)
        mix.columnconfigure(1, weight=1)
        self.mix_path = self._field(mix, 0, "Latest recipe path", str(self._restore_path("save_recipe_path", OUTPUT_DIR / "mixing_recipe.csv")))
        manual_mix = self.config.get("manual_mixing_settings", {})
        self.staple = self._field(mix, 1, "Base staple stock [nM, shared ×TE, Mg mM, Na mM]", str(manual_mix.get("staple", "{:g},1,0,0".format(self.base_stock_concentration_nm()))))
        self.scaffold = self._field(mix, 2, "Scaffold stock [nM, ×TE, Mg mM, Na mM]", str(manual_mix.get("scaffold", "400,0.1,0,0")))
        self.te_10x = self._field(mix, 3, "10X TE [nM, xTE, Mg, Na]", str(manual_mix.get("te_10x", "0,10,0,0")))
        self.magnesium = self._field(mix, 4, "Mg [nM, xTE, mM, Na]", str(manual_mix.get("magnesium", "0,0,100,0")))
        self.sodium = self._field(mix, 5, "Na [nM, xTE, Mg, mM]", str(manual_mix.get("sodium", "0,0,0,100")))
        self.desired = tk.StringVar(value=str(self.config.get("manual_desired_mix", "100,50,10,1,12,5")))
        self.manual_recipe_target_summary = tk.StringVar()
        ttk.Label(mix, text="Final synthesis targets (from Replacements)").grid(row=6,column=0,sticky="e",padx=6,pady=4)
        ttk.Label(mix,textvariable=self.manual_recipe_target_summary).grid(row=6,column=1,sticky="w",padx=6)
        self.manual_final_buffer = self._field(mix,7,"Final buffer target [×TE, Mg mM, Na mM]", ",".join(self.desired.get().split(",")[3:]))
        syncing_buffer = [False]
        def sync_recipe_targets(*_args):
            if syncing_buffer[0]:return
            parts=self.desired.get().split(",")
            syncing_buffer[0]=True
            try:
                if len(parts)==6:
                    self.manual_recipe_target_summary.set("{} µL · book-base {} nM · scaffold {} nM".format(*parts[:3]))
                    self.manual_final_buffer.set(",".join(parts[3:]))
                else:
                    self.manual_recipe_target_summary.set("Invalid synthesis settings; edit in Replacements.")
            finally:syncing_buffer[0]=False
        def update_buffer_target(*_args):
            if syncing_buffer[0]:return
            parts=self.desired.get().split(",")
            self.desired.set(",".join(parts[:3])+","+self.manual_final_buffer.get())
        self.desired.trace_add("write",sync_recipe_targets)
        self.manual_final_buffer.trace_add("write",update_buffer_target)
        sync_recipe_targets()
        ttk.Label(mix,text="The fields above describe stock solutions and buffer targets. Synthesis volume and DNA targets are set in Replacements.",wraplength=800).grid(row=8,column=0,columnspan=3,sticky="w",pady=5)
        targets = ttk.LabelFrame(mix, text="Replacement-site targets (from Replacements)", padding=8)
        targets.grid(row=9, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(targets, text="Read-only defaults. Per-site overrides and replacement percentages remain as selected in Replacements.",wraplength=800).grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        for index, view in enumerate(self.set_views, 1):
            ttk.Label(targets, text="{} (nM; stock {:g} µM)".format(view.title, view.concentration_nm / 1000)).grid(row=index, column=0, sticky="w", padx=(0, 12))
            ttk.Label(targets, textvariable=view.desired_concentration_nm).grid(row=index, column=1, sticky="w", pady=2)
        ttk.Button(targets,text="Edit synthesis / replacement targets…",command=lambda:self.main_tabs.select(self.replacement_page)).grid(
            row=len(self.set_views)+1,column=0,columnspan=2,sticky="w",pady=5)

        action = ttk.Frame(page, padding=14)
        action.grid(row=2, column=0, sticky="ew")
        self.status = tk.StringVar(value="Ready")
        ttk.Button(action, text="Open latest output folder", command=self._open_latest_output).pack(side="left", padx=10)
        ttk.Label(action, textvariable=self.status).pack(side="left", padx=10)

    def _build_preview(self, page: ttk.Frame) -> None:
        top = ttk.Frame(page, padding=10)
        top.pack(fill="x")
        self.summary = tk.StringVar(value="Generate a run to see results.")
        ttk.Label(top, textvariable=self.summary).pack(side="left", fill="x", expand=True)
        self.send_storage_button = ttk.Button(
            top, text="Send to storage", command=self._add_latest_to_storage
        )
        self.send_storage_button.pack(side="right")
        self.save_run_button = ttk.Button(
            top, text="Save picklist + recipe", command=self._save_latest_run
        )
        self.save_run_button.pack(side="right", padx=(0, 8))
        self.send_storage_button.configure(state="disabled")
        self.save_run_button.configure(state="disabled")
        notebook = ttk.Notebook(page)
        notebook.pack(fill="both", expand=True, padx=10, pady=5)
        pick_page, mix_page = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(pick_page, text="Picklist preview")
        notebook.add(mix_page, text="Mixing recipe")
        self.pick_tree = self._tree(pick_page, PICKLIST_COLUMNS)
        self.mix_tree = self._tree(mix_page, ("Reagent", "Volume_uL"))

    def _build_storage(self, page: ttk.Frame) -> None:
        top = ttk.Frame(page, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(
            top,
            text="Stored runs remain available after restarting. Select rows to combine CSVs or export selections.",
        ).pack(side="left", fill="x", expand=True)

        actions = ttk.Frame(page, padding=(12, 0, 12, 8))
        actions.pack(fill="x")
        ttk.Button(actions, text="Select all", command=self._select_all_storage).pack(side="left")
        ttk.Button(
            actions,
            text="Clear selection",
            command=lambda: self.storage_tree.selection_remove(self.storage_tree.selection()),
        ).pack(side="left", padx=6)
        ttk.Button(actions, text="Remove selected", command=self._remove_selected_storage).pack(
            side="left", padx=(12, 6)
        )
        ttk.Button(
            actions,
            text="Generate selections PDF",
            command=self._generate_storage_selections_pdf,
        ).pack(side="right", padx=(6, 0))
        ttk.Button(
            actions,
            text="Generate combined picklist + recipes",
            style="Generate.TButton",
            command=self._generate_combined_storage,
        ).pack(side="right")

        tree_frame = ttk.Frame(page, padding=(12, 0, 12, 6))
        tree_frame.pack(fill="both", expand=True)
        columns = ("Created", "Run name", "Replacements", "Destination plate", "Destination wells", "Transfers")
        self.storage_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        widths = (145, 190, 270, 140, 180, 80)
        for column, width in zip(columns, widths):
            self.storage_tree.heading(column, text=column)
            self.storage_tree.column(column, width=width, stretch=True)
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.storage_tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.storage_tree.xview)
        self.storage_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.storage_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.storage_status = tk.StringVar()
        ttk.Label(page, textvariable=self.storage_status, padding=(12, 4, 12, 10)).pack(fill="x")
        self.storage_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_storage_status())
        self._refresh_storage_tree()

    def _storage_items(self) -> List[Dict[str, object]]:
        items = self.storage_state.setdefault("items", [])
        if not isinstance(items, list):
            items = []
            self.storage_state["items"] = items
        return items

    def _refresh_storage_tree(self) -> None:
        if not hasattr(self, "storage_tree"):
            return
        selected = set(self.storage_tree.selection())
        self.storage_tree.delete(*self.storage_tree.get_children())
        for item_index, item in enumerate(self._storage_items(), 1):
            item_id = str(item.get("id", ""))
            if not item_id:
                continue
            names = item.get("design_names", [])
            design = ", ".join(str(name) for name in names) if isinstance(names, list) else str(names)
            run_name = str(item.get("run_name", "")).strip() or "Stored run {}".format(item_index)
            wells = item.get("destination_wells", [])
            well_text = ",".join(str(well) for well in wells) if isinstance(wells, list) else str(wells)
            created = str(item.get("created_utc", "")).replace("T", " ")[:19]
            self.storage_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    created,
                    run_name,
                    design,
                    item.get("destination_plate_name", ""),
                    well_text,
                    item.get("transfer_count", 0),
                ),
            )
        existing = [item_id for item_id in selected if self.storage_tree.exists(item_id)]
        if existing:
            self.storage_tree.selection_set(existing)
        elif self.storage_tree.get_children():
            self.storage_tree.selection_set(self.storage_tree.get_children())
        self._update_storage_status()

    def _update_storage_status(self) -> None:
        count = len(self._storage_items())
        selected = len(self.storage_tree.selection()) if hasattr(self, "storage_tree") else 0
        self.storage_status.set(
            "{} stored run{} • {} selected".format(count, "" if count == 1 else "s", selected)
        )

    def _select_all_storage(self) -> None:
        children = self.storage_tree.get_children()
        if children:
            self.storage_tree.selection_set(children)

    def _add_latest_to_storage(self) -> None:
        if not self.last_picklist or not self.last_mix:
            messagebox.showinfo(
                "Nothing to store", "Generate a picklist and mixing recipe first.", parent=self
            )
            return
        if self.last_run_stored or any(
            item.get("source_run_id") == self.last_run_id for item in self._storage_items()
        ):
            messagebox.showinfo(
                "Already stored", "This preview is already in storage.", parent=self
            )
            return
        try:
            self.barcode_guard.check_preview()
        except ValueError as exc:
            messagebox.showerror("Barcode check failed", str(exc), parent=self)
            return
        destination_wells = list(
            dict.fromkeys(str(row["Destination Well"]) for row in self.last_picklist)
        )
        destination_plate = str(self.last_picklist[0].get("Destination Plate Name", ""))
        suggested_name = "{} {}".format(destination_plate, ",".join(destination_wells))
        run_name = simpledialog.askstring(
            "Name stored run",
            "Enter a name for this picklist run:",
            initialvalue=suggested_name,
            parent=self,
        )
        if run_name is None:
            return
        run_name = run_name.strip()
        if not run_name:
            messagebox.showerror("Run name required", "Enter a name for the stored run.", parent=self)
            return
        selection_panels = self.last_selection_panels or self._replacement_selection_panels(
            set(self.last_design_names)
        )
        if not selection_panels:
            messagebox.showerror(
                "Missing selection metadata",
                "The latest run's replacement selections could not be captured. Generate the run again.",
                parent=self,
            )
            return
        item = {
            "id": uuid4().hex,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "run_name": run_name,
            "design_names": list(self.last_design_names),
            "hinge_type": self.last_hinge_type,
            "selection_panels": [dict(panel) for panel in selection_panels],
            "destination_plate_name": destination_plate,
            "destination_wells": destination_wells,
            "transfer_count": len(self.last_picklist),
            "source_run_id": self.last_run_id,
            "source_picklist_path": (
                self._portable_path(self.picklist_path.get()) if self.last_run_saved else ""
            ),
            "source_recipe_path": (
                self._portable_path(self.mix_path.get()) if self.last_run_saved else ""
            ),
            "picklist": [dict(row) for row in self.last_picklist],
            "mixing_recipe": [dict(row) for row in self.last_mix],
        }
        try:
            self.barcode_guard.reserve_manual(run_name)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Cannot reserve barcode", str(exc), parent=self)
            return
        item["barcode_record"] = deepcopy(getattr(self, "last_barcode_record", None))
        self._storage_items().append(item)
        self.storage_state["version"] = 2
        self.storage_state["updated_utc"] = item["created_utc"]
        save_json(STORAGE_STATE_PATH, self.storage_state)
        self._refresh_storage_tree()
        self._select_all_storage()
        self.last_run_stored = True
        self._commit_latest_run()
        self._update_result_action_buttons()
        self.status.set("'{}' added to storage".format(run_name))

    def _update_result_action_buttons(self) -> None:
        if not hasattr(self, "save_run_button"):
            return
        has_preview = bool(self.last_picklist and self.last_mix)
        if not has_preview and self.main_tabs.select() == str(self.preview_page):
            fallback = (self.settings_page if self.main_tabs.tab(self.settings_page, "state") == "normal"
                        else self.replacement_page)
            self.main_tabs.select(fallback)
        self.main_tabs.tab(self.preview_page, state="normal" if has_preview else "disabled")
        self._sync_navigation()
        self.save_run_button.configure(
            state="normal" if has_preview and not self.last_run_saved else "disabled"
        )
        self.send_storage_button.configure(
            state="normal" if has_preview and not self.last_run_stored else "disabled"
        )

    def _commit_latest_run(self) -> None:
        """Record destination usage once, whether the preview is saved or stored first."""
        if self.last_run_committed:
            return
        if not self.last_picklist:
            raise ValueError("Generate a preview first.")
        record_transfers(self.plate_state, self.last_picklist)
        save_json(PLATE_STATE_PATH, self.plate_state)
        used_destination_wells = list(
            dict.fromkeys(str(row["Destination Well"]) for row in self.last_picklist)
        )
        destination_plate = str(
            self.last_picklist[0].get("Destination Plate Name", "Destination[1]")
        )
        self.config["recent_destination_wells"] = used_destination_wells
        next_destination_wells = next_unused_wells(
            self.plate_state, destination_plate, len(used_destination_wells) or 1
        )
        self.starting_well.set("")
        # Advancing to the next run's wells must preserve this committed preview
        # so it can still be saved after storage (or stored after saving).
        self._advancing_destination = True
        try:
            self.destination_wells.set(",".join(next_destination_wells))
        finally:
            self._advancing_destination = False
        self.config["last_committed_run"] = {
            "committed_utc": self.plate_state.get("updated_utc"),
            "run_id": self.last_run_id,
            "transfer_count": len(self.last_picklist),
            "destination_plate_name": destination_plate,
            "destination_wells": used_destination_wells,
        }
        self.last_run_committed = True
        self._flush_config()

    def _save_latest_run(self) -> None:
        if not self.last_picklist or not self.last_mix:
            messagebox.showinfo(
                "Nothing to save", "Generate a picklist and mixing recipe preview first.", parent=self
            )
            return
        if self.last_run_saved:
            messagebox.showinfo("Already saved", "This preview has already been saved.", parent=self)
            return
        try:
            self.barcode_guard.reserve_manual()
            output_root = Path(self.output_root.get()).expanduser()
            if not output_root.is_absolute():
                output_root = APP_DIR / output_root
            picklist_filename = Path(self.picklist_path.get()).name or "picklist_combined.csv"
            recipe_filename = Path(self.mix_path.get()).name or "mixing_recipe.csv"
            if picklist_filename == recipe_filename:
                raise ValueError("Picklist and recipe filenames must be different.")
            run_directory = create_run_output_directory(output_root)
            picklist_output = run_directory / picklist_filename
            recipe_output = run_directory / recipe_filename
            write_csv(picklist_output, self.last_picklist, PICKLIST_COLUMNS)
            write_csv(recipe_output, self.last_mix, ("Reagent", "Volume_uL"))
            self.picklist_path.set(str(picklist_output))
            self.mix_path.set(str(recipe_output))
            self.last_run_saved = True
            self._commit_latest_run()
            storage_updated = False
            for item in self._storage_items():
                if item.get("source_run_id") == self.last_run_id:
                    item["source_picklist_path"] = self._portable_path(self.picklist_path.get())
                    item["source_recipe_path"] = self._portable_path(self.mix_path.get())
                    storage_updated = True
            if storage_updated:
                self.storage_state["updated_utc"] = datetime.now(timezone.utc).isoformat()
                save_json(STORAGE_STATE_PATH, self.storage_state)
            used_destination_wells = list(
                dict.fromkeys(str(row["Destination Well"]) for row in self.last_picklist)
            )
            self.config["last_successful_generation"] = {
                "generated_utc": self.plate_state.get("updated_utc"),
                "run_id": self.last_run_id,
                "transfer_count": len(self.last_picklist),
                "destination_plate_name": str(
                    self.last_picklist[0].get("Destination Plate Name", "")
                ),
                "destination_wells": used_destination_wells,
                "picklist_path": self._portable_path(self.picklist_path.get()),
                "recipe_path": self._portable_path(self.mix_path.get()),
            }
            self._flush_config()
            self._update_result_action_buttons()
            self.status.set("Saved picklist and recipe in {}".format(run_directory.name))
        except Exception as exc:
            self.status.set("Save failed")
            messagebox.showerror("Could not save files", str(exc), parent=self)

    def _selected_storage_items(self) -> List[Dict[str, object]]:
        selected = set(self.storage_tree.selection())
        return [item for item in self._storage_items() if str(item.get("id", "")) in selected]

    def _remove_selected_storage(self) -> None:
        selected = self._selected_storage_items()
        if not selected:
            messagebox.showinfo(
                "Nothing selected", "Select one or more stored runs to remove.", parent=self
            )
            return
        if not messagebox.askyesno(
            "Remove stored runs",
            "Remove {} selected run{} from storage? Generated CSV files will not be deleted.".format(
                len(selected), "" if len(selected) == 1 else "s"
            ),
            icon="warning",
            parent=self,
        ):
            return
        selected_ids = {str(item.get("id", "")) for item in selected}
        self.storage_state["items"] = [
            item for item in self._storage_items() if str(item.get("id", "")) not in selected_ids
        ]
        self.storage_state["updated_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(STORAGE_STATE_PATH, self.storage_state)
        self._refresh_storage_tree()

    def _generate_storage_selections_pdf(self) -> None:
        selected = self._selected_storage_items()
        if not selected:
            messagebox.showinfo(
                "Nothing selected", "Select one or more stored runs, or click Select all.", parent=self
            )
            return
        runs = []
        upgraded = False
        for item_index, item in enumerate(selected, 1):
            panels = item.get("selection_panels", [])
            if not isinstance(panels, list) or not panels:
                names = item.get("design_names", [])
                name_set = {str(name) for name in names} if isinstance(names, list) else set()
                panels = self._replacement_selection_panels(name_set)
                if panels:
                    item["selection_panels"] = panels
                    upgraded = True
            run_name = str(item.get("run_name", "")).strip() or "Stored run {}".format(item_index)
            if not panels:
                messagebox.showerror(
                    "Missing selection metadata",
                    "Cannot reconstruct replacement selections for '{}'. Regenerate and store that run again.".format(
                        run_name
                    ),
                    parent=self,
                )
                return
            runs.append({"run_name": run_name, "panels": panels})
        if upgraded:
            self.storage_state["version"] = 2
            self.storage_state["updated_utc"] = datetime.now(timezone.utc).isoformat()
            save_json(STORAGE_STATE_PATH, self.storage_state)

        try:
            run_directory = self._shared_export_directory()
            path = run_directory / "stored_run_selections.pdf"
            write_stored_runs_selection_pdf(path, runs)
            messagebox.showinfo(
                "Stored run selections PDF saved",
                "Saved selections for {} run{} to:\n{}".format(
                    len(runs), "" if len(runs) == 1 else "s", path
                ),
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("Could not save stored run selections PDF", str(exc), parent=self)

    def _generate_combined_storage(self) -> None:
        selected = self._selected_storage_items()
        if not selected:
            messagebox.showinfo(
                "Nothing selected", "Select one or more stored runs, or click Select all.", parent=self
            )
            return
        try:
            picklists = [item.get("picklist", []) for item in selected]
            recipes = [item.get("mixing_recipe", []) for item in selected]
            combined_picklist = combine_picklists(picklists)  # type: ignore[arg-type]
            separated_recipes = separate_mixing_recipes(recipes)  # type: ignore[arg-type]
            run_directory = self._shared_export_directory()
            picklist_output = run_directory / "picklist_combined.csv"
            write_csv(picklist_output, combined_picklist, PICKLIST_COLUMNS)
            for index, (item, recipe) in enumerate(zip(selected, separated_recipes), 1):
                run_name = str(item.get("run_name", "")).strip() or "run_{}".format(index)
                safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", run_name).strip("_")[:80]
                safe_name = safe_name or "run_{}".format(index)
                recipe_output = run_directory / "mixing_recipe_{:02d}_{}.csv".format(
                    index, safe_name
                )
                write_csv(recipe_output, recipe, ("Reagent", "Volume_uL"))
            self.storage_status.set(
                "Combined {} runs into {} transfers and {} separate recipes; files saved in {}".format(
                    len(selected), len(combined_picklist), len(separated_recipes), run_directory.name
                )
            )
            messagebox.showinfo(
                "Combined files generated",
                "Saved picklist_combined.csv and {} separate mixing recipe{} in:\n{}".format(
                    len(separated_recipes),
                    "" if len(separated_recipes) == 1 else "s",
                    run_directory,
                ),
                parent=self,
            )
        except Exception as exc:
            messagebox.showerror("Could not combine stored runs", str(exc), parent=self)

    def _tree(self, parent: tk.Widget, columns: Sequence[str]) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=150 if column != "Sample Comments" else 300, stretch=True)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _selected_hinge_type(self) -> str:
        return next(key for key, label in HINGE_TYPES.items() if label == self.hinge_type.get())

    def generate(self, tabs: ttk.Notebook, preview_page: ttk.Frame) -> None:
        try:
            if not self.check_selections():
                self.status.set("Generation stopped — resolve selection clashes")
                tabs.select(self.replacement_page)
                return
            guard = getattr(self, "barcode_guard", None)
            barcode_entry = guard.check_manual() if guard else None
            selections = []
            design_names = []
            desired = parse_vector(self.desired.get(), 6, "Desired")
            for view in self.set_views:
                names = view.selected()
                if names:
                    try:
                        target = view.desired_concentration_nm.get().strip()
                        desired_concentration = float(target)
                        desired_concentration_ratio(desired_concentration, desired[1])
                    except ValueError:
                        raise ValueError("{}: desired base and replacement concentrations must be finite positive numbers.".format(view.title))
                    for path, plate, stock, source_names in SetView.replacement_sources(view, names):
                        selections.append(ReplacementSelection(
                            path, source_names, plate,
                            replacement_tab=view.title,
                            panel_names={
                                name: str(panel["label"])
                                for panel in view.panels
                                for row in panel["rows"]
                                for column in panel["columns"]
                                for name in (getattr(view, "resolved_name", lambda value: value)(panel_sequence_name(panel, row, column)),)
                                if name in source_names
                            },
                            site_fractions={name: view.site_fractions.get(name, 1.0) for name in source_names},
                            concentration_nm=stock,
                            desired_concentration_nm=desired_concentration,
                            site_concentrations_nm={name: view.site_concentrations_nm[name]
                                                    for name in source_names if name in view.site_concentrations_nm},
                        ))
                    design_names.extend(names)
            if not selections:
                raise ValueError("Select at least one replacement.")
            transfer_volume = self._manual_base_transfer() if hasattr(self,"_manual_base_transfer") else float(self.transfer_volume.get())
            transfers_per_source = 1 if hasattr(self,"_manual_base_transfer") else int(self.transfers_per_source.get())
            transfer_limits = self._manual_transfer_limits() if hasattr(self,"_manual_transfer_limits") else {}
            destination_wells = valid_well_list(self.destination_wells.get())
            staple = parse_vector(self.staple.get(), 4, "Base staple")
            self.destination_wells.set(",".join(destination_wells))
            picklist = generate_picklist(
                Path(self.base_path.get()), selections, destination_wells,
                self.source_plate_type.get().strip(), self.destination_plate.get().strip(),
                transfer_volume, float(self.max_volume.get()), transfers_per_source,
                hinge_type=self._selected_hinge_type(),
                base_concentration_nm=staple[0],
                base_desired_concentration_nm=desired[1],
                **transfer_limits,
            )
            unique_sources = {(str(row["Source Plate Name"]), str(row["Source Well"])) for row in picklist}
            total_volume_nl = sum(float(row["Transfer Volume"]) for row in picklist)
            available_ul = total_volume_nl / 1000.0
            # Transfers already compensate for each stock concentration. The
            # physical pool volume per base transfer sets the aliquot needed for
            # the base target concentration, including plate targets and dilute stocks.
            # All staple stocks share the buffer composition in the staple vector.
            effective_staples = total_volume_nl / (transfer_volume * transfers_per_source)
            mixing = calculate_mixing_volumes(
                staple, effective_staples,
                parse_vector(self.scaffold.get(), 4, "Scaffold"),
                parse_vector(self.magnesium.get(), 4, "Mg"), parse_vector(self.sodium.get(), 4, "Na"),
                parse_vector(self.te_10x.get(), 4, "10X TE"), desired,
            )
            requested_ul = mixing[0]["Volume_uL"]
            if requested_ul > available_ul + 1e-9:
                raise ValueError("Requested staple volume ({:.3f} µL) exceeds picklist availability ({:.3f} µL).".format(requested_ul, available_ul))
            self.last_picklist, self.last_mix = picklist, mixing
            self.last_design_names = design_names
            self.last_hinge_type = self._selected_hinge_type()
            self.last_selection_panels = self._replacement_selection_panels()
            self.last_run_id = uuid4().hex
            self.last_barcode_record = barcode_entry
            if barcode_entry:
                barcode_entry.update(id=self.last_run_id, destination_plate_name=self.destination_plate.get().strip(),
                                     destination_wells=list(dict.fromkeys(row["Destination Well"] for row in picklist)))
            self.last_run_committed = False
            self.last_run_saved = False
            self.last_run_stored = False
            self._update_result_action_buttons()
            self._fill_tree(self.pick_tree, picklist[:500], PICKLIST_COLUMNS)
            self._fill_tree(self.mix_tree, mixing, ("Reagent", "Volume_uL"))
            self.summary.set("{} transfers • {} unique staples • {:.3f} µL available • {:.3f} µL requested".format(len(picklist), len(unique_sources), available_ul, requested_ul))
            self.summary.set(self.summary.get() + " • " + HINGE_TYPES[self.last_hinge_type])
            self.status.set("Preview ready — choose Save picklist + recipe or Send to storage")
            tabs.select(preview_page)
        except Exception as exc:
            self.status.set("Generation failed")
            messagebox.showerror("Could not generate preview", str(exc), parent=self)

    @staticmethod
    def _fill_tree(tree: ttk.Treeview, rows: Sequence[Dict[str, object]], columns: Sequence[str]) -> None:
        tree.delete(*tree.get_children())
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column, "")
                values.append("{:.6g}".format(value) if isinstance(value, float) else value)
            tree.insert("", "end", values=values)

    def _open_latest_output(self) -> None:
        latest = Path(self.picklist_path.get()).expanduser()
        folder = latest.parent if latest.name else Path(self.output_root.get()).expanduser()
        if not folder.is_absolute():
            folder = APP_DIR / folder
        self._open_path(folder)

    @staticmethod
    def _open_path(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])


if __name__ == "__main__":
    PicklistApp().mainloop()
