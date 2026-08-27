"""Desktop GUI for creating Echo picklists and mixing recipes."""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from collections import defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app_state import (
    PLATE_ROWS,
    all_384_wells,
    clear_plate,
    create_run_output_directory,
    load_json,
    next_unused_wells,
    normalize_recent_wells,
    plate_wells,
    record_transfers,
    save_json,
    unused_wells_from,
)
from picklist_core import (
    PICKLIST_COLUMNS,
    ReplacementSelection,
    calculate_mixing_volumes,
    generate_picklist,
    parse_vector,
    parse_source2,
    replacement_names,
    valid_well_list,
    write_csv,
)


APP_DIR = Path(__file__).resolve().parent
SHEET_DIR = APP_DIR / "replacement_sheets"
OUTPUT_DIR = APP_DIR / "generated_output"
CONFIG_PATH = APP_DIR / "config.json"
PLATE_STATE_PATH = APP_DIR / "destination_plate_state.json"
CONFIG_EXAMPLE_PATH = APP_DIR / "config.example.json"
PLATE_STATE_EXAMPLE_PATH = APP_DIR / "destination_plate_state.example.json"

DEFAULT_CONFIG = {
    "version": 1,
    "save_picklist_path": "generated_output/picklist_combined.csv",
    "save_recipe_path": "generated_output/mixing_recipe.csv",
    "output_root_path": "generated_output",
    "pending_destination_wells": ["A01", "A02", "A03"],
    "recent_destination_wells": [],
    "destination_plate_name": "Destination[1]",
    "source_plate_type": "384PP_AQ_BP",
    "transfers_per_source": 1,
    "transfer_volume_nL": 50,
    "max_destination_volume_uL": 12.5,
}
DEFAULT_PLATE_STATE = {"version": 1, "plates": {}}

ROWS = ["H01", "H03", "H05", "H07", "H09", "H11", "H13", "H15"]
COLS = ["{:02d}".format(i) for i in range(1, 13)]


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
    ) -> None:
        self.title = title
        self.path_var = tk.StringVar(value=str(csv_path))
        self.plate_name = plate_name
        self.variables: Dict[str, tk.BooleanVar] = {}
        self.buttons: Dict[str, tk.Checkbutton] = {}
        self.color_frames: Dict[str, tk.Frame] = {}
        self.selectable: Dict[str, bool] = {}
        self.available: Set[str] = set()
        self.conflicts: Set[str] = set()
        self.selection_changed = selection_changed
        self.panels = panels

        scroll = ScrollFrame(parent)
        scroll.pack(fill="both", expand=True)
        toolbar = ttk.Frame(scroll.inner)
        toolbar.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(toolbar, text="{} replacement sheet".format(title)).pack(side="left")
        ttk.Entry(toolbar, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(toolbar, text="Browse…", command=self._browse).pack(side="left")
        ttk.Button(toolbar, text="Clear set", command=self.clear).pack(side="left", padx=(8, 0))
        panel_bar = ttk.Frame(scroll.inner)
        panel_bar.pack(fill="x", padx=12, pady=(5, 2))
        ttk.Label(panel_bar, text="Panel:").pack(side="left")
        self.panel_var = tk.StringVar(value=str(self.panels[0]["label"]))
        self.panel_picker = ttk.Combobox(
            panel_bar,
            textvariable=self.panel_var,
            values=[str(panel["label"]) for panel in self.panels],
            state="readonly",
            width=24,
        )
        self.panel_picker.pack(side="left", padx=(6, 12))
        self.panel_picker.bind("<<ComboboxSelected>>", lambda _event: self._show_panel())
        self.selection_summary = tk.StringVar(value="0 selected in this set")
        ttk.Label(panel_bar, textvariable=self.selection_summary, foreground="#555555").pack(side="left")
        if self.title in {"Aptamers", "MB", "PAINT P1", "PAINT R1"}:
            legend = tk.Frame(scroll.inner, background="#f0f0f0")
            legend.pack(fill="x", padx=12, pady=(4, 2))
            self._legend_square(legend, self.SELECTABLE_BG, "").pack(side="left")
            tk.Label(legend, text=" Selectable", background="#f0f0f0").pack(side="left", padx=(0, 14))
            self._legend_square(legend, self.SELECTED_BG, "✓", foreground="white").pack(side="left")
            tk.Label(legend, text=" Selected", background="#f0f0f0").pack(side="left", padx=(0, 14))
            self._legend_square(legend, self.UNAVAILABLE_BG, "×", foreground="#62676b").pack(side="left")
            tk.Label(legend, text=" Not selectable", background="#f0f0f0").pack(side="left")
        self.panel_host = ttk.Frame(scroll.inner)
        self.panel_host.pack(fill="both", expand=True, padx=10, pady=6)
        self.refresh()

    def _browse(self) -> None:
        path = filedialog.askopenfilename(title="Choose replacement CSV", filetypes=[("CSV files", "*.csv")])
        if path:
            self.path_var.set(path)
            self.refresh()

    def refresh(self) -> None:
        self.variables.clear()
        self.buttons.clear()
        self.color_frames.clear()
        self.selectable.clear()
        self.conflicts.clear()
        try:
            self.available = replacement_names(Path(self.path_var.get()))
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
        self._show_panel()

    def _show_panel(self) -> None:
        for child in self.panel_host.winfo_children():
            child.destroy()
        self.buttons.clear()
        self.color_frames.clear()
        selected_label = self.panel_var.get()
        panel = next((item for item in self.panels if str(item["label"]) == selected_label), self.panels[0])
        self._build_panel(panel, self.available)
        self._update_selection_summary()

    def _build_panel(self, panel: Dict[str, object], available: Set[str]) -> None:
        label = str(panel["label"])
        frame = ttk.LabelFrame(self.panel_host, text=label)
        frame.pack(fill="x", pady=5)
        rows = list(panel["rows"])
        columns = list(panel["columns"])
        active = panel.get("active")
        colors = panel.get("colors", {})
        unavailable_in_panel = []

        header_row = 0
        ttk.Label(frame, text="").grid(row=header_row, column=0, padx=3)
        for ci, column in enumerate(columns, 1):
            ttk.Label(frame, text=str(column), anchor="center").grid(row=header_row, column=ci, padx=3)
        for ri, row in enumerate(rows, 1):
            grid_row = header_row + ri
            ttk.Label(frame, text=str(row), width=6, anchor="e").grid(row=grid_row, column=0, padx=(4, 8))
            for ci, column in enumerate(columns, 1):
                position = (str(row), str(column))
                name = panel_sequence_name(panel, row, column)
                configured = active is None or position in active
                exists = name in available
                is_selectable = configured and exists
                variable = self.variables.get(name, tk.BooleanVar(value=False))
                color = colors.get(position) if isinstance(colors, dict) else None
                color_frame = None
                button_parent = frame
                if color:
                    color_frame = tk.Frame(frame, background=str(color), padx=2, pady=2)
                    color_frame.grid(row=grid_row, column=ci, padx=(3, 12 if ci == 6 else 3), pady=2)
                    button_parent = color_frame
                button = tk.Checkbutton(
                    button_parent,
                    text="" if is_selectable else "×",
                    variable=variable,
                    indicatoron=False,
                    width=2,
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
                    button.grid(row=grid_row, column=ci, padx=(3, 12 if ci == 6 else 3), pady=2)
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
        button = self.buttons.get(name)
        variable = self.variables.get(name)
        if button is None or variable is None or not self.selectable.get(name, False):
            return
        selected = variable.get()
        conflict = selected and name in self.conflicts
        button.configure(
            text="×" if conflict else ("✓" if selected else ""),
            background=self.CONFLICT_BG if conflict else (self.SELECTED_BG if selected else self.SELECTABLE_BG),
            activebackground=self.CONFLICT_BG if conflict else (self.SELECTED_BG if selected else self.SELECTABLE_HOVER_BG),
            foreground="#ffffff" if selected else "#222222",
            relief="sunken" if selected else "raised",
        )
        self._update_selection_summary()

    def _on_square_toggled(self, name: str) -> None:
        self.conflicts.clear()
        self._paint_square(name)
        if self.selection_changed is not None:
            self.selection_changed()

    def _hover_square(self, name: str, entering: bool) -> None:
        button = self.buttons.get(name)
        variable = self.variables.get(name)
        if button is None or variable is None or variable.get() or not self.selectable.get(name, False):
            return
        button.configure(background=self.SELECTABLE_HOVER_BG if entering else self.SELECTABLE_BG)

    def selected(self) -> List[str]:
        return [name for name, variable in self.variables.items() if variable.get()]

    def _update_selection_summary(self) -> None:
        count = sum(variable.get() for variable in self.variables.values())
        self.selection_summary.set("{} selected in this set".format(count))

    def mark_conflicts(self, names: Set[str]) -> None:
        self.conflicts = set(names)
        for name in self.buttons:
            self._paint_square(name)

    def clear(self) -> None:
        self.conflicts.clear()
        for name, variable in self.variables.items():
            variable.set(False)
            self._paint_square(name)
        self._update_selection_summary()
        if self.selection_changed is not None:
            self.selection_changed()


def panel_sequence_name(panel: Dict[str, object], row: object, column: object) -> str:
    template = str(panel.get("name_template", "{label}_{row}-{column}"))
    return template.format(label=panel["label"], row=row, column=column)


def dense_panel(
    label: str,
    active: Optional[Set[Tuple[str, str]]] = None,
    colors=None,
    name_template: Optional[str] = None,
) -> Dict[str, object]:
    panel: Dict[str, object] = {
        "label": label,
        "rows": ROWS,
        "columns": COLS,
        "active": active,
        "colors": colors or {},
    }
    if name_template is not None:
        panel["name_template"] = name_template
    return panel


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
    return [
        ("Yaritza Extensions", "yaritza_replace.csv", "SourcePlate3[3]", [dense_panel("D-Apt"), dense_panel("U-Apt")]),
        ("Aptamers", "max_replace.csv", "SourcePlate4[4]", c_panels),
        ("MB", "max_replace_MB.csv", "SourcePlate4[4]", [dense_panel("D-MB", set_d), dense_panel("U-MB", set_u)]),
        ("PAINT P1", "PAINT_replace.csv", "SourcePlate5[5]", [dense_panel("D-Biotin", set_e), dense_panel("U-PAINT", colors=colors)]),
        (
            "PAINT R1",
            "PAINT_R1_replace.csv",
            "SourcePlate6[6]",
            [dense_panel("U-Apt R1", colors=paint_r1_colors, name_template="U-Apt_{row}-{column}_R1")],
        ),
    ]


class DestinationPlateView(ttk.Frame):
    CELL_WIDTH = 42
    CELL_HEIGHT = 29
    LEFT = 38
    TOP = 32

    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, padding=10)
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
        self.canvas.bind("<Button-1>", self._clicked)
        self.current_plate = ""
        self.current_wells: Dict[str, Dict] = {}
        self.capacity_nl = 12500

    def render(self, plate_name: str, state: Dict, entered_wells: Sequence[str], capacity_ul: float) -> None:
        self.current_plate = plate_name
        self.current_wells = plate_wells(state, plate_name)
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
                    outline="#59636b",
                    tags=("well", well),
                )
                self.canvas.create_text(
                    x + self.CELL_WIDTH / 2,
                    y + self.CELL_HEIGHT / 2,
                    text=well,
                    font=("TkDefaultFont", 7),
                    tags=("well", well),
                )
        width = self.LEFT + 24 * self.CELL_WIDTH + 8
        height = self.TOP + 16 * self.CELL_HEIGHT + 8
        self.canvas.configure(scrollregion=(0, 0, width, height))

    def _clicked(self, event) -> None:
        column = int((self.canvas.canvasx(event.x) - self.LEFT) // self.CELL_WIDTH) + 1
        row_index = int((self.canvas.canvasy(event.y) - self.TOP) // self.CELL_HEIGHT)
        if column not in range(1, 25) or row_index not in range(16):
            return
        well = "{}{:02d}".format(PLATE_ROWS[row_index], column)
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


class PicklistApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        config_defaults = load_json(CONFIG_EXAMPLE_PATH, DEFAULT_CONFIG)
        plate_defaults = load_json(PLATE_STATE_EXAMPLE_PATH, DEFAULT_PLATE_STATE)
        self.config = load_json(CONFIG_PATH, config_defaults)
        self.plate_state = load_json(PLATE_STATE_PATH, plate_defaults)
        if not CONFIG_PATH.exists():
            save_json(CONFIG_PATH, self.config)
        if not PLATE_STATE_PATH.exists():
            save_json(PLATE_STATE_PATH, self.plate_state)
        self._config_save_job = None
        self.title("Echo Picklist Generator")
        self.geometry("1280x880")
        self.minsize(960, 700)
        self._set_style()
        self.set_views: List[SetView] = []
        self.last_picklist: List[Dict[str, object]] = []
        self.last_mix: List[Dict[str, float]] = []
        self._build()

    def _set_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Generate.TButton", font=("TkDefaultFont", 11, "bold"), padding=8)

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="Echo Picklist Generator", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Select replacements, configure the run, then generate both CSV files.").pack(side="left", padx=18)
        main_tabs = ttk.Notebook(self)
        main_tabs.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        replacement_page = ttk.Frame(main_tabs)
        settings_page = ttk.Frame(main_tabs)
        plate_page = ttk.Frame(main_tabs)
        preview_page = ttk.Frame(main_tabs)
        main_tabs.add(replacement_page, text="1. Replacements")
        main_tabs.add(settings_page, text="2. Run & Mixing Settings")
        main_tabs.add(plate_page, text="3. Destination Plate")
        main_tabs.add(preview_page, text="4. Results")
        self.main_tabs = main_tabs
        self.replacement_page = replacement_page

        self._build_compatibility_footer(replacement_page)
        set_tabs = ttk.Notebook(replacement_page)
        set_tabs.pack(side="top", fill="both", expand=True)
        for title, filename, plate_name, panels in panel_definitions():
            page = ttk.Frame(set_tabs)
            set_tabs.add(page, text=title)
            self.set_views.append(
                SetView(
                    page,
                    title,
                    SHEET_DIR / filename,
                    plate_name,
                    panels,
                    selection_changed=self._selections_changed,
                )
            )

        self._build_settings(settings_page, main_tabs, preview_page)
        plate_actions = ttk.Frame(plate_page, padding=(12, 10, 12, 0))
        plate_actions.pack(fill="x")
        ttk.Button(plate_actions, text="Start fresh plate", command=self._start_fresh_plate).pack(side="left")
        ttk.Label(
            plate_actions,
            text="Clears recorded usage for the currently named destination plate; generated files are not deleted.",
        ).pack(side="left", padx=12)
        self.plate_view = DestinationPlateView(plate_page)
        self.plate_view.pack(fill="both", expand=True)
        self._build_preview(preview_page)
        self._bind_config_updates()
        self._refresh_plate_view()
        self.protocol("WM_DELETE_WINDOW", self._close_app)

    def _build_compatibility_footer(self, page: ttk.Frame) -> None:
        footer = ttk.LabelFrame(page, text="Selection compatibility", padding=8)
        footer.pack(side="bottom", fill="x", padx=6, pady=(4, 6))
        top = ttk.Frame(footer)
        top.pack(fill="x")
        ttk.Button(top, text="Check selections", command=self.check_selections).pack(side="left")
        self.compatibility_status = tk.Label(
            top,
            text="Not checked",
            foreground="#555555",
            background="#f0f0f0",
        )
        self.compatibility_status.pack(side="left", padx=12)
        self.clash_text = tk.Text(
            footer,
            height=4,
            wrap="word",
            relief="solid",
            borderwidth=1,
            background="#ffffff",
            foreground="#333333",
        )
        self.clash_text.pack(fill="x", pady=(6, 0))
        self._set_clash_text("Select replacement wells, then click Check selections.")

    def _set_clash_text(self, text: str) -> None:
        self.clash_text.configure(state="normal")
        self.clash_text.delete("1.0", "end")
        self.clash_text.insert("1.0", text)
        self.clash_text.configure(state="disabled")

    def _selections_changed(self) -> None:
        for view in self.set_views:
            view.mark_conflicts(set())
        self.compatibility_status.configure(text="Selections changed — check again", foreground="#8a5a00")
        self._set_clash_text("Compatibility results are out of date. Click Check selections to run the check again.")

    def check_selections(self) -> bool:
        """Highlight selections that replace the same base well and list every clash."""
        targets = defaultdict(list)
        conflicts_by_view = {view: set() for view in self.set_views}
        try:
            for view in self.set_views:
                selected_names = set(view.selected())
                if not selected_names:
                    continue
                rows, _plate_name = parse_source2(Path(view.path_var.get()))
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
        )
        for variable in variables:
            variable.trace_add("write", lambda *_args: self._schedule_config_save())

    def _schedule_config_save(self) -> None:
        if self._config_save_job is not None:
            self.after_cancel(self._config_save_job)
        self._config_save_job = self.after(300, self._flush_config)

    def _flush_config(self) -> None:
        self._config_save_job = None
        self.config.update(
            {
                "version": 1,
                "save_picklist_path": self._portable_path(self.picklist_path.get()),
                "save_recipe_path": self._portable_path(self.mix_path.get()),
                "output_root_path": self._portable_path(self.output_root.get()),
                "pending_destination_wells": normalize_recent_wells(self.destination_wells.get().split(",")),
                "destination_plate_name": self.destination_plate.get().strip() or "Destination[1]",
                "source_plate_type": self.source_plate_type.get().strip(),
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
            "Clear all recorded well usage for '{}'?\n\nGenerated output folders will not be deleted.".format(plate_name),
            icon="warning",
            parent=self,
        )
        if not confirmed:
            return
        count = self._destination_well_count()
        clear_plate(self.plate_state, plate_name)
        save_json(PLATE_STATE_PATH, self.plate_state)
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
        page.columnconfigure(0, weight=1)
        pick = ttk.LabelFrame(page, text="Picklist", padding=10)
        pick.grid(row=0, column=0, sticky="new", padx=14, pady=10)
        pick.columnconfigure(1, weight=1)
        configured_plate = str(self.config.get("destination_plate_name", "Destination[1]"))
        recent = normalize_recent_wells(self.config.get("recent_destination_wells", []))
        pending = normalize_recent_wells(self.config.get("pending_destination_wells", ["A01", "A02", "A03"]))
        default_wells = next_unused_wells(self.plate_state, configured_plate, len(recent) or len(pending) or 3)
        self.base_path = self._field(pick, 0, "Base Source1", str(SHEET_DIR / "book_base.csv"))
        self.destination_wells = self._field(pick, 1, "Destination wells", ",".join(default_wells))
        self.transfers_per_source = self._field(pick, 2, "Transfers/source", str(self.config.get("transfers_per_source", 1)))
        self.transfer_volume = self._field(pick, 3, "Transfer volume (nL)", str(self.config.get("transfer_volume_nL", 50)))
        self.max_volume = self._field(pick, 4, "Max destination volume (µL)", str(self.config.get("max_destination_volume_uL", 12.5)))
        self.destination_plate = self._field(pick, 5, "Destination plate", configured_plate)
        self.source_plate_type = self._field(pick, 6, "Source plate type", str(self.config.get("source_plate_type", "384PP_AQ_BP")))
        self.output_root = self._field(pick, 7, "Generated output root", str(self._restore_path("output_root_path", OUTPUT_DIR)))
        self.picklist_path = self._field(pick, 8, "Latest picklist path", str(self._restore_path("save_picklist_path", OUTPUT_DIR / "picklist_combined.csv")))
        self.starting_well = self._field(pick, 9, "Destination starting well", "")
        ttk.Button(pick, text="Start here", command=self._apply_starting_well).grid(row=9, column=2, sticky="w", padx=6, pady=4)

        mix = ttk.LabelFrame(page, text="Mixing recipe", padding=10)
        mix.grid(row=1, column=0, sticky="new", padx=14, pady=6)
        mix.columnconfigure(1, weight=1)
        self.mix_path = self._field(mix, 0, "Latest recipe path", str(self._restore_path("save_recipe_path", OUTPUT_DIR / "mixing_recipe.csv")))
        self.staple = self._field(mix, 1, "Staple [nM, xTE, Mg, Na]", "200000,1,0,0")
        self.scaffold = self._field(mix, 2, "Scaffold [nM, xTE, Mg, Na]", "400,0.1,0,0")
        self.te_10x = self._field(mix, 3, "10X TE [nM, xTE, Mg, Na]", "0,10,0,0")
        self.magnesium = self._field(mix, 4, "Mg [nM, xTE, mM, Na]", "0,0,100,0")
        self.sodium = self._field(mix, 5, "Na [nM, xTE, Mg, mM]", "0,0,0,100")
        self.desired = self._field(mix, 6, "Desired [µL, staple nM, scaffold nM, xTE, Mg mM, Na mM]", "500,10,1,1,12,5")

        action = ttk.Frame(page, padding=14)
        action.grid(row=2, column=0, sticky="ew")
        self.status = tk.StringVar(value="Ready")
        ttk.Button(action, text="Generate Picklist + Mixing Recipe", style="Generate.TButton", command=lambda: self.generate(tabs, preview_page)).pack(side="left")
        ttk.Button(action, text="Open latest output folder", command=self._open_latest_output).pack(side="left", padx=10)
        ttk.Label(action, textvariable=self.status).pack(side="left", padx=10)

    def _build_preview(self, page: ttk.Frame) -> None:
        self.summary = tk.StringVar(value="Generate a run to see results.")
        ttk.Label(page, textvariable=self.summary, padding=10).pack(fill="x")
        notebook = ttk.Notebook(page)
        notebook.pack(fill="both", expand=True, padx=10, pady=5)
        pick_page, mix_page = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(pick_page, text="Picklist preview")
        notebook.add(mix_page, text="Mixing recipe")
        self.pick_tree = self._tree(pick_page, PICKLIST_COLUMNS)
        self.mix_tree = self._tree(mix_page, ("Reagent", "Volume_uL"))

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

    def generate(self, tabs: ttk.Notebook, preview_page: ttk.Frame) -> None:
        try:
            if not self.check_selections():
                self.status.set("Generation stopped — resolve selection clashes")
                tabs.select(self.replacement_page)
                return
            selections = []
            for view in self.set_views:
                names = view.selected()
                if names:
                    selections.append(ReplacementSelection(Path(view.path_var.get()), names, view.plate_name))
            if not selections:
                raise ValueError("Select at least one replacement.")
            transfer_volume = int(self.transfer_volume.get())
            transfers_per_source = int(self.transfers_per_source.get())
            picklist = generate_picklist(
                Path(self.base_path.get()), selections, valid_well_list(self.destination_wells.get()),
                self.source_plate_type.get().strip(), self.destination_plate.get().strip(),
                transfer_volume, float(self.max_volume.get()), transfers_per_source,
            )
            unique_sources = {(str(row["Source Plate Name"]), str(row["Source Well"])) for row in picklist}
            available_ul = len(unique_sources) * transfer_volume * transfers_per_source / 1000.0
            mixing = calculate_mixing_volumes(
                parse_vector(self.staple.get(), 4, "Staple"), len(unique_sources),
                parse_vector(self.scaffold.get(), 4, "Scaffold"),
                parse_vector(self.magnesium.get(), 4, "Mg"), parse_vector(self.sodium.get(), 4, "Na"),
                parse_vector(self.te_10x.get(), 4, "10X TE"), parse_vector(self.desired.get(), 6, "Desired"),
            )
            requested_ul = mixing[0]["Volume_uL"]
            if requested_ul > available_ul + 1e-9:
                raise ValueError("Requested staple volume ({:.3f} µL) exceeds picklist availability ({:.3f} µL).".format(requested_ul, available_ul))
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
            write_csv(picklist_output, picklist, PICKLIST_COLUMNS)
            write_csv(recipe_output, mixing, ("Reagent", "Volume_uL"))
            self.picklist_path.set(str(picklist_output))
            self.mix_path.set(str(recipe_output))
            record_transfers(self.plate_state, picklist)
            save_json(PLATE_STATE_PATH, self.plate_state)
            used_destination_wells = list(
                dict.fromkeys(str(row["Destination Well"]) for row in picklist)
            )
            self.config["recent_destination_wells"] = used_destination_wells
            next_destination_wells = next_unused_wells(
                self.plate_state,
                self.destination_plate.get().strip() or "Destination[1]",
                len(used_destination_wells) or 1,
            )
            self.starting_well.set("")
            self.destination_wells.set(",".join(next_destination_wells))
            self.config["last_successful_generation"] = {
                "generated_utc": self.plate_state.get("updated_utc"),
                "transfer_count": len(picklist),
                "destination_plate_name": self.destination_plate.get().strip(),
                "destination_wells": used_destination_wells,
                "picklist_path": self._portable_path(self.picklist_path.get()),
                "recipe_path": self._portable_path(self.mix_path.get()),
            }
            self._flush_config()
            self.last_picklist, self.last_mix = picklist, mixing
            self._fill_tree(self.pick_tree, picklist[:500], PICKLIST_COLUMNS)
            self._fill_tree(self.mix_tree, mixing, ("Reagent", "Volume_uL"))
            self.summary.set("{} transfers • {} unique staples • {:.3f} µL available • {:.3f} µL requested".format(len(picklist), len(unique_sources), available_ul, requested_ul))
            if next_destination_wells:
                self.status.set(
                    "Success — files saved in {}; next destination wells: {}".format(
                        run_directory.name, ",".join(next_destination_wells)
                    )
                )
            else:
                self.status.set("Success — files saved in {}; destination plate is full".format(run_directory.name))
            tabs.select(preview_page)
        except Exception as exc:
            self.status.set("Generation failed")
            messagebox.showerror("Could not generate files", str(exc), parent=self)

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
