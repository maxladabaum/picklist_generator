"""Visualize fixed-color and joint-code tiling sweeps at multiple Gaussian widths."""
import math
import numpy as np
import tkinter as tk
from tkinter import ttk

from image_encoding import SWEEP_SCHEMES, DEFAULT_SCHEME, bin_image, geometry, physical_reference_raster

FAMILY_FILTERS = {
    '{} positions / {} types ({} layouts)'.format(budget, 3*budget,
        sum(s.parent_scheme == parent for s in SWEEP_SCHEMES.values())): parent
    for budget, parent in ((16, DEFAULT_SCHEME), (18, 'joint_6x3'), (21, 'joint_7x3'), (20, 'joint_5x4'))}
LAYOUT_FILTERS = ("24×32 samples (16 layouts)", "All allocations ({} layouts)".format(len(SWEEP_SCHEMES)),
                  *FAMILY_FILTERS, *("{} tiles".format(n) for n in sorted(
                      {scheme.tile_count for scheme in SWEEP_SCHEMES.values()})))


def parse_widths(text):
    try:
        widths = list(dict.fromkeys(float(item.strip()) for item in text.split(",")))
    except ValueError:
        raise ValueError("Enter comma-separated FWHM values in nm, such as 5, 7.5, 10.") from None
    if not 1 <= len(widths) <= 6 or any(not math.isfinite(w) or w <= 0 for w in widths):
        raise ValueError("Enter one to six distinct, finite, positive FWHM values in nm.")
    return widths


def selected_layouts(choice):
    if choice not in LAYOUT_FILTERS:
        raise ValueError("Choose a layout group.")
    layouts = list(SWEEP_SCHEMES.items())
    if choice == LAYOUT_FILTERS[0]:
        layouts = [(key, s) for key, s in layouts if s.x_states == s.y_states == 4 and s.parent_scheme == DEFAULT_SCHEME]
    elif choice in FAMILY_FILTERS:
        layouts = [(key, s) for key, s in layouts if s.parent_scheme == FAMILY_FILTERS[choice]]
    elif choice != LAYOUT_FILTERS[1]:
        layouts = [(key, s) for key, s in layouts if s.tile_count == int(choice.split()[0])]
    return sorted(layouts, key=lambda item: (item[1].tile_count, item[1].tile_columns,
                                            item[1].x_states, item[0]))


class TilingComparison(tk.Toplevel):
    def __init__(self, owner):
        super().__init__(owner.winfo_toplevel())
        self.owner = owner
        self.title("XY shifts, tiles, and Gaussian width")
        self.geometry("1180x780")
        self.minsize(650, 420)
        self.stale = True
        self.results = {}
        self.photos = []
        self.status = tk.StringVar()
        self.settings = tk.StringVar()
        self.widths = tk.StringVar(value="5, 7.5, 10")
        self.layout_filter = tk.StringVar(value=LAYOUT_FILTERS[0])
        ttk.Label(self, text="Shifts versus tiles", font=("TkDefaultFont", 16, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(self, wraplength=1120, text=(
            "Six-bit layouts use 48 types with fixed color bits, or joint 6×3 / 7×3 / 5×4 budgets of 54 / 63 / 60 types. "
            "Uneven partitions distribute remaining local shifts across tiles; each family keeps its total type count. "
            "Rows change the tile/shift allocation; columns change Gaussian FWHM. "
            "All physical origami remain 6×8 sites. Previews fit the same display box with physical aspect preserved; "
            "white lines on the fitted target mark tile boundaries. The image stays upright.")).pack(anchor="w", padx=12)
        controls = ttk.Frame(self)
        controls.pack(fill="x", padx=12, pady=8)
        ttk.Combobox(controls, textvariable=self.layout_filter, values=LAYOUT_FILTERS,
                     state="readonly", width=29).pack(side="left")
        ttk.Label(controls, text="FWHM values (nm)").pack(side="left", padx=(14, 5))
        ttk.Entry(controls, textvariable=self.widths, width=25).pack(side="left")
        self.refresh_button = ttk.Button(controls, text="Render comparison", command=self.refresh)
        self.refresh_button.pack(side="left", padx=10)
        ttk.Label(self, textvariable=self.settings, wraplength=1120).pack(anchor="w", padx=12)
        ttk.Label(self, textvariable=self.status, wraplength=1120).pack(anchor="w", padx=12, pady=4)
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=12, pady=(0, 8))
        host = ttk.Frame(self)
        host.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.viewport = tk.Canvas(host, highlightthickness=0)
        horizontal = ttk.Scrollbar(host, orient="horizontal", command=self.viewport.xview)
        vertical = ttk.Scrollbar(host, orient="vertical", command=self.viewport.yview)
        self.viewport.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.viewport.grid(row=0, column=0, sticky="nsew")
        horizontal.grid(row=1, column=0, sticky="ew")
        vertical.grid(row=0, column=1, sticky="ns")
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)
        self.cards = ttk.Frame(self.viewport)
        self.viewport.create_window(0, 0, window=self.cards, anchor="nw")
        self.cards.bind("<Configure>", lambda _event: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        self.widths.trace_add("write", self.mark_stale)
        self.layout_filter.trace_add("write", self.mark_stale)

    def mark_stale(self, *_args):
        self.stale = True
        self.results.clear()
        self.photos.clear()
        for child in self.cards.winfo_children():
            child.destroy()
        self.status.set("Settings changed. Click Render comparison to update all previews.")
        self.progress.configure(value=0)

    def _picture(self, parent, source, info, tile_grid=False):
        from PIL import Image, ImageTk
        preview = source.copy()
        preview.thumbnail((240, 180), Image.Resampling.LANCZOS)
        canvas = tk.Canvas(parent, width=252, height=196, background="#15191d", highlightthickness=0)
        canvas.pack()
        photo = ImageTk.PhotoImage(preview, master=canvas)
        self.photos.append(photo)
        left, top = (252-preview.width)/2, (186-preview.height)/2
        canvas.create_image(left, top, anchor="nw", image=photo)
        if tile_grid:
            tx, ty = info["tile_columns"], info["tile_rows"]
            if info["origami_rotation_degrees_clockwise"] in (90, 270):
                tx, ty = ty, tx
            for i in range(1, tx):
                x = left + preview.width*i/tx
                canvas.create_line(x, top, x, top+preview.height, fill="white", dash=(2, 2))
            for i in range(1, ty):
                y = top + preview.height*i/ty
                canvas.create_line(left, y, left+preview.width, y, fill="white", dash=(2, 2))
        bar = preview.width * 20 / info["display_field_width_nm"]
        canvas.create_line(8, 187, 8+bar, 187, fill="white", width=2)
        canvas.create_text(12+bar, 187, text="20 nm", fill="white", anchor="w")
        return preview

    def use_layout(self, key):
        self.owner.scheme.set(SWEEP_SCHEMES[key].label)
        self.owner.winfo_toplevel().lift()

    def refresh(self):
        from PIL import Image
        owner = self.owner
        self.mark_stale()
        self.refresh_button.configure(state="disabled")
        self.configure(cursor="watch")
        try:
            widths = parse_widths(self.widths.get())
            layouts = selected_layouts(self.layout_filter.get())
            if owner.pixels is None:
                raise ValueError("Upload a valid image first.")
            rotation = int(owner.rotation.get())
            options = owner._light_options()
            summary = ("Optimization {} for each layout/FWHM independently. {} · origami {}° · exposure {}× · "
                       "base {} nL · plate targets {}. Linear light {}; equal total signal {}; area brightness matching {}.").format(
                "ON" if owner.optimize_enabled.get() else "OFF", owner.fit_mode.get(), rotation,
                owner.gaussian_exposure.get(), owner.app.transfer_volume.get(),
                ", ".join("{} {} nM".format(c,v.desired_concentration_nm.get()) for c,v in owner.color_views.items()),
                "ON" if options["linear_light"] else "OFF", "ON" if options["normalize_designs"] else "OFF",
                "ON" if options["compensate_area"] else "OFF")
            if owner.alignment_enabled.get():
                settings = owner._alignment_settings()
                summary += (" Expected alignment: X/Y SD {translation_x_nm:g}/{translation_y_nm:g} nm, "
                            "angle SD {rotation_deg:g}°, N={origami_per_design}, center "
                            "({center_x_nm:g}, {center_y_nm:g}) nm, variability penalty {variability_penalty:g}.").format(**vars(settings))
            else:
                summary += " Alignment uncertainty OFF."
            summary += " Regularization {} (0 = off).".format(owner.regularization.get())
            self.settings.set(summary + " FWHM columns override only the main tab’s Gaussian width. Area matching changes exposure, not photon counts.")
            total = len(layouts)*len(widths)
            self.progress.configure(maximum=total, value=0)
            ttk.Label(self.cards, text="Layout and fitted target", font=("TkDefaultFont", 12, "bold")).grid(row=0, column=0, pady=8)
            for column, width in enumerate(widths, 1):
                ttk.Label(self.cards, text="FWHM {:g} nm".format(width), font=("TkDefaultFont", 12, "bold")).grid(row=0, column=column, pady=8)
            completed, failures = 0, 0
            for row, (key, scheme) in enumerate(layouts, 1):
                info = geometry(key, rotation, owner._active_columns())
                pixels = bin_image(owner.image_path.get(), owner.fit_mode.get(), rotation, key, linear_light=options["linear_light"], active_image_columns=owner._active_columns())
                baseline = owner._compile_scheme(pixels, key)
                frame = ttk.LabelFrame(self.cards, text=scheme.label, padding=5)
                frame.grid(row=row, column=0, sticky="n", padx=5, pady=5)
                bit_counts = ((scheme.tile_columns-1).bit_length(), (scheme.tile_rows-1).bit_length(),
                              (scheme.x_counts[0]-1).bit_length(), (scheme.y_counts[0]-1).bit_length())
                code_detail = ("Joint six-bit codebook · local X {} / Y {}\n".format(scheme.x_counts, scheme.y_counts)
                               if scheme.joint else "Bits: tile X/Y {} / {} · shift X/Y {} / {}\n".format(*bit_counts))
                if scheme.position_codebook:
                    code_detail = "2 color bits + 4 position-code bits · local X {} / Y {}\n".format(scheme.x_counts, scheme.y_counts)
                ttk.Label(frame, text=code_detail + (
                    "{display_columns}×{display_rows} samples · {design_count} types\n"
                    "{display_field_width_nm:g}×{display_field_height_nm:g} nm field\n"
                    "{display_pixel_pitch_x_nm:g}×{display_pixel_pitch_y_nm:g} nm spacing").format(*bit_counts, **info)).pack()
                target = physical_reference_raster(Image.fromarray(np.asarray(pixels, dtype='uint8')), key, rotation, owner._active_columns())
                self._picture(frame, target, info, tile_grid=True)
                ttk.Button(frame, text="Use this layout", command=lambda key=key: self.use_layout(key)).pack(pady=4)
                for column, width in enumerate(widths, 1):
                    self.status.set("{} / {} complete · {} · FWHM {:g} nm · {}…".format(
                        completed, total, scheme.label, width, "optimizing and rendering" if owner.optimize_enabled.get() else "rendering"))
                    self.update_idletasks()
                    cell = ttk.Frame(self.cards, padding=5)
                    cell.grid(row=row, column=column, sticky="n", padx=5, pady=5)
                    try:
                        designs, metrics = baseline, None
                        if owner.optimize_enabled.get():
                            designs, metrics = owner._optimize(baseline, fwhm_nm=width)
                        result = owner._render_overlay(designs, fwhm_nm=width)
                        preview = self._picture(cell, result.rgb, info)
                        self.results[key, width] = {"image": preview, "metadata": result.metadata, "optimization": metrics}
                        detail = "FWHM / spacing: {:.2f}×{:.2f}\nClipped {:.1%} · area exposure {:g}×".format(
                            width/info["display_pixel_pitch_x_nm"], width/info["display_pixel_pitch_y_nm"],
                            result.metadata["clipped_pixel_fraction"], result.metadata["canvas_area_gain"])
                        if metrics is not None:
                            detail += "\nOptimized RMSE {:.4f} → {:.4f}".format(metrics["baseline_rmse"], metrics["optimized_rmse"])
                        ttk.Label(cell, text=detail).pack()
                        # Only thumbnails/metadata survive a cell; large density rasters do not.
                        del result
                    except (ValueError, ArithmeticError) as exc:
                        failures += 1
                        ttk.Label(cell, text="Cannot render this cell:\n{}".format(exc), wraplength=240).pack()
                    completed += 1
                    self.progress.configure(value=completed)
                    self.update_idletasks()
            self.stale = False
            self.status.set("Comparison complete: {} layouts × {} FWHM values. {}{}".format(
                len(layouts), len(widths), "Scroll vertically and horizontally to compare.",
                " {} cells could not render; see their messages.".format(failures) if failures else ""))
        except Exception as exc:
            self.mark_stale()
            self.status.set("Cannot compare: {}".format(exc))
        finally:
            self.refresh_button.configure(state="normal")
            self.configure(cursor="")
