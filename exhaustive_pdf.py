"""Bounded-memory PDF report with the best-fitting orientation for each encoding."""
from dataclasses import dataclass, field, replace
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import math
import os
import shutil
import tempfile
import textwrap

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from alignment_reconstruction import AlignmentModel, AlignmentSettings, VALIDATION_SEED
from gaussian_reconstruction import render_gaussian_overlay
from gaussian_optimization import fitted_target, optimize_designs
from image_encoding import SELECTABLE_SCHEMES, CHANNELS, FIT_MODES, active_columns, validate_barcode_schema, fiducial_sites, bin_image, compile_designs, geometry, physical_reference_raster, best_rotation, rotate_point
from image_encoding import rgb_transfer_grid
from optimization_regularization import DEFAULT_REGULARIZATION, validate_regularization

PANEL_TITLES = ('Binned RGB', 'Before optimization — Gaussian overlay', 'Predicted RGB',
                'Ideal alignment', 'Expected alignment', 'Finite count')
ROTATIONS = (0, 90)
PAGE_SIZE = (1754, 1240)  # Landscape A4 at 150 dpi.


class ExportCancelled(Exception):
    pass


@dataclass(frozen=True)
class ReportSettings:
    image_path: str
    fit_mode: str
    base_volume: int
    multipliers: dict
    fwhm_nm: float
    exposure: float
    linear_light: bool = True
    normalize_designs: bool = True
    compensate_area: bool = True
    optimize: bool = False
    alignment_enabled: bool = False
    alignment: AlignmentSettings = field(default_factory=AlignmentSettings)
    example_seed: int = 42
    regularization_strength: float = DEFAULT_REGULARIZATION

    barcode_schema: dict | None = None
    active_image_columns: list | None = None
    plate_relative_transfers: bool = False
    plate_defaults_nm: dict | None = None
    base_desired_concentration_nm: float = 50.0
    base_concentration_nm: float = 200000.0
    source_concentrations_nm: dict | None = None
    minimum_transfer_nl: float = 25.0
    transfer_increment_nl: float = 25.0

    def concentration_options(self):
        return dict(minimum_transfer_nl=self.minimum_transfer_nl, transfer_increment_nl=self.transfer_increment_nl,
                    plate_defaults_nm=self.plate_defaults_nm,
                    active_image_columns=self.active_image_columns,
                    barcode_schema=self.barcode_schema,
                    plate_relative_transfers=self.plate_relative_transfers,
                    base_desired_concentration_nm=self.base_desired_concentration_nm,
                    base_concentration_nm=self.base_concentration_nm,
                    source_concentrations_nm=self.source_concentrations_nm)

    def validate(self):
        active_columns(self.active_image_columns)
        fiducial_sites(self.barcode_schema, self.active_image_columns)
        if self.barcode_schema is not None:
            validate_barcode_schema(self.barcode_schema)
        if not Path(self.image_path).is_file():
            raise ValueError('Upload a readable image first.')
        if self.fit_mode not in FIT_MODES:
            raise ValueError('Choose a valid image fitting mode.')
        if not all(math.isfinite(value) and value > 0 for value in (self.fwhm_nm, self.exposure)):
            raise ValueError('Gaussian FWHM and exposure must be finite and positive.')
        self.alignment.validate()
        validate_regularization(self.regularization_strength)
        for color in CHANNELS:
            target = (self.base_desired_concentration_nm * self.multipliers[color]
                      if self.plate_defaults_nm is None else self.plate_defaults_nm[color])
            rgb_transfer_grid(self.base_volume, target, target if self.plate_relative_transfers else self.base_desired_concentration_nm,
                              self.base_concentration_nm, (self.source_concentrations_nm or {}).get(color, 200000.0),
                              self.minimum_transfer_nl, self.transfer_increment_nl)

    def light_options(self):
        return dict(linear_light=self.linear_light, normalize_designs=self.normalize_designs,
                    compensate_area=self.compensate_area)


def report_jobs(image_path, schemes=None, active_image_columns=None):
    with Image.open(image_path) as source:
        image_size = ImageOps.exif_transpose(source).size
    return [(key, best_rotation(image_size, key, active_image_columns))
            for key in (SELECTABLE_SCHEMES if schemes is None else schemes)]


def generate_views(settings, scheme_id, rotation, checkpoint=lambda phase: None, score_only=False):
    """Same assignments and models as the RGB tab; no Tk access or UI mutation."""
    checkpoint('Binning image')
    info = geometry(scheme_id, rotation, settings.active_image_columns, settings.barcode_schema)
    options = settings.light_options()
    pixels = bin_image(settings.image_path, settings.fit_mode, rotation, scheme_id, linear_light=settings.linear_light, active_image_columns=settings.active_image_columns)
    designs = compile_designs(pixels, settings.base_volume, settings.multipliers, scheme_id, rotation,
                              linear_light=settings.linear_light, **settings.concentration_options())
    binned = physical_reference_raster(Image.fromarray(np.asarray(pixels, dtype=np.uint8)), scheme_id, rotation, settings.active_image_columns)
    checkpoint('Building alignment model')
    model = AlignmentModel(designs, settings.alignment, settings.fwhm_nm, settings.exposure, **options)
    checkpoint('Rendering before optimization')
    before = (model.expected(designs) if settings.alignment_enabled else
              render_gaussian_overlay(designs, settings.fwhm_nm, settings.exposure, **options))
    before_rgb, before_meta = before.rgb, before.metadata
    metrics = None
    original = designs
    validation = None
    if settings.optimize:
        checkpoint('Optimizing replacement percentages')
        target = fitted_target(settings.image_path, settings.fit_mode, before.rgb.size, linear_light=settings.linear_light)
        if settings.alignment_enabled:
            original = designs
            designs, metrics = model.optimize(designs, target, regularization_strength=settings.regularization_strength)
            checkpoint('Validating alignment optimization')
            validation = AlignmentModel(original, settings.alignment, settings.fwhm_nm, settings.exposure,
                                        seed=VALIDATION_SEED, **options)
            metrics['validation_seed'] = VALIDATION_SEED
            metrics['validation_baseline'] = validation.evaluate(original, target)
            metrics['validation_optimized'] = validation.evaluate(designs, target)
            metrics['validation_used_for_selection'] = False
            if not score_only:
                del validation
        else:
            designs, metrics = optimize_designs(designs, target, settings.fwhm_nm, settings.exposure, **options, regularization_strength=settings.regularization_strength)
    del before
    if score_only:
        checkpoint('Scoring independent prediction')
        from similarity_graph import score_assignments
        score = score_assignments(settings, info, original, designs, validation)
        return (score, designs) if score_only == 'assignments' else score
    checkpoint('Rendering ideal alignment')
    ideal = render_gaussian_overlay(designs, settings.fwhm_nm, settings.exposure, **options)
    ideal_rgb, ideal_meta = ideal.rgb, ideal.metadata
    del ideal
    checkpoint('Rendering expected alignment')
    expected = model.expected(designs)
    expected_rgb, expected_meta = expected.rgb, expected.metadata
    del expected
    checkpoint('Rendering finite-count example')
    example = model.example(designs, settings.example_seed)
    predicted_rgb = expected_rgb if settings.alignment_enabled else ideal_rgb
    predicted_meta = expected_meta if settings.alignment_enabled else ideal_meta
    return dict(geometry=info, optimization=metrics,
                grid_design=designs[0],
                images=[binned, before_rgb, predicted_rgb, ideal_rgb, expected_rgb, example.rgb],
                metadata=[None, before_meta, predicted_meta, ideal_meta, expected_meta, example.metadata])


def _font(size):
    for name in ('/System/Library/Fonts/Supplemental/Arial.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def settings_lines(settings):
    return [
        'Source: ' + Path(settings.image_path).name,
        'Barcode schema: ' + (settings.barcode_schema or {}).get('name', 'Standard barcode sites'),
        'Always-on fiducials: ' + (', '.join((settings.barcode_schema or {}).get('always_on_sites', [])) or 'none'),
        'Active analog rows: ' + ', '.join(active_columns(settings.active_image_columns)),
        'Fit: {} | Gaussian FWHM: {:g} nm | Shared exposure: {:g}x'.format(settings.fit_mode, settings.fwhm_nm, settings.exposure),
        ('Base transfer: {} nL | Plate targets: {}'.format(settings.base_volume,
            ', '.join('{} {:g} nM'.format(c, settings.plate_defaults_nm[c]) for c in CHANNELS))
         if settings.plate_defaults_nm is not None else
         'Base transfer: {} nL | Legacy excess: {}'.format(settings.base_volume, ', '.join('{} {:g}x'.format(c, settings.multipliers[c]) for c in CHANNELS))),
        'Linear light: {} | Equal total signal: {} | Canvas brightness compensation: {}'.format(settings.linear_light, settings.normalize_designs, settings.compensate_area),
        'Replacement optimization: {} | Alignment used for prediction/optimization: {}'.format(settings.optimize, settings.alignment_enabled),
        'Alignment SD: X {translation_x_nm:g} nm, Y {translation_y_nm:g} nm, angle {rotation_deg:g} degrees'.format(**vars(settings.alignment)),
        'Alignment center: ({center_x_nm:g}, {center_y_nm:g}) nm | Origami/design: {origami_per_design} | Variability penalty: {variability_penalty:g}'.format(**vars(settings.alignment)),
        'Regularization: {:g} (0 = off); penalizes departure from direct assignments. Target: fitted uploaded image.'.format(settings.regularization_strength),
        'Finite-count seed: {} | Held-out optimization seed: {}'.format(settings.example_seed, VALIDATION_SEED),
    ]


def cover_page(settings, count):
    page = Image.new('RGB', PAGE_SIZE, 'white')
    draw = ImageDraw.Draw(page)
    draw.text((60, 55), 'Exhaustive RGB encoding report', font=_font(40), fill='black')
    lines = ['Generated ' + datetime.now().astimezone().isoformat(timespec='seconds'),
             '{} layouts: {} view pages plus this settings page.'.format(count, count),
             'Each layout uses 0 or 90 degrees, whichever best matches the upright source aspect ratio; ties use 0 degrees.',
             'Each view page contains all six panels. The source image remains upright.', ''] + settings_lines(settings) + ['',
             'Binned RGB: fitted source samples with correct physical aspect ratio. White outlines on all six panels mark the 48 nominal pixels of the first design for each layout; the design is named on the page.',
             'Before optimization: direct replacement assignment, with the same alignment mode as Predicted RGB.',
             'Predicted RGB: the active assignments, optimized only when the optimization option is enabled.',
             'Ideal alignment: active assignments with no residual alignment error.',
             'Expected alignment: active assignments averaged over sampled residual rigid transforms.',
             'Finite count: one seeded realization with the configured number of origami per design.', '',
             'Expected and finite-count alignment panels always use the entered error settings, even when alignment is disabled for the main prediction.',
             'When optimization is OFF, before/after assignments are identical. Predicted RGB matches either Ideal or Expected alignment according to the alignment setting.',
             'Each layout and rotation is independently binned and, when enabled, optimized at the current FWHM. The FWHM comparison window does not override this report.',
             'Panels retain the full fitted image with its natural aspect ratio. For Fit with black padding, only the known fitting margins are trimmed; real dark content is retained. All six panels share the same physical region and scale. Headers list full fields; scale bars show 20 nm.',
             'Canvas brightness compensation changes exposure, not photon counts. Finite-count simulation includes alignment uncertainty only, not occupancy or photon noise.',
             'All dropdown entries are included, including equivalent layouts under different names. No runs are prepared or modified by this export.']
    y = 120
    for line in lines:
        for wrapped in textwrap.wrap(line, width=135) or ['']:
            draw.text((60, y), wrapped, font=_font(21), fill='#202020')
            y += 29
    return page



def display_region(settings, info):
    """Crop only mathematically known fitting padding, never dark image content."""
    width_nm, height_nm = info['display_field_width_nm'], info['display_field_height_nm']
    fraction_x = fraction_y = 1.0
    if settings.fit_mode == FIT_MODES[0]:
        with Image.open(settings.image_path) as source:
            width, height = ImageOps.exif_transpose(source).size
        source_aspect, field_aspect = width/height, width_nm/height_nm
        fraction_x = min(1.0, source_aspect/field_aspect)
        fraction_y = min(1.0, field_aspect/source_aspect)
    left, top = (1-fraction_x)/2, (1-fraction_y)/2
    return (left, top, 1-left, 1-top), (width_nm*fraction_x, height_nm*fraction_y)


def draw_design_grid(preview, info, region, design):
    """Outline one design's nominal bins, in the same frame as the RGB tab.

    Draw before pasting so Pillow clips outlines to the retained source region.
    This annotation never changes the reconstruction or optimization rasters.
    """
    draw = ImageDraw.Draw(preview)
    span_x, span_y = info['display_field_width_nm'], info['display_field_height_nm']
    def point(x, y):
        x, y = rotate_point(x, y, info['field_width_nm'], info['field_height_nm'],
                            info['origami_rotation_degrees_clockwise'])
        return ((x/span_x-region[0])*preview.width/(region[2]-region[0]),
                (y/span_y-region[1])*preview.height/(region[3]-region[1]))
    for site in design['image_sites']:
        x1, x2 = info['pixel_edges_x_nm'][site['pixel_x']:site['pixel_x']+2]
        y1, y2 = info['pixel_edges_y_nm'][site['pixel_y']:site['pixel_y']+2]
        a, b = point(x1, y1), point(x2, y2)
        bounds = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        draw.rectangle(bounds, outline='black', width=3)
        draw.rectangle(bounds, outline='white', width=1)


def view_page(settings, views, page_number, total):
    page = Image.new('RGB', PAGE_SIZE, 'white')
    draw = ImageDraw.Draw(page)
    info = views['geometry']
    draw.text((40, 25), info['encoding_label'], font=_font(28), fill='black')
    subtitle = ('Origami {origami_rotation_degrees_clockwise} degrees clockwise | {display_columns} x {display_rows} samples | '
                '{design_count} designs | {display_field_width_nm:g} x {display_field_height_nm:g} nm').format(**info)
    draw.text((40, 66), subtitle, font=_font(21), fill='#202020')
    draw.text((40, 98), 'FWHM {:g} nm | Exposure {:g}x | Optimization {} | Prediction alignment {}'.format(
        settings.fwhm_nm, settings.exposure, 'ON' if settings.optimize else 'OFF',
        'ON' if settings.alignment_enabled else 'OFF'), font=_font(21), fill='#202020')
    region, (width_nm, height_nm) = display_region(settings, info)
    draw.text((40, 128), 'Full image content; fitting padding trimmed | Shown region {:g} x {:g} nm'.format(
        width_nm, height_nm) if settings.fit_mode == FIT_MODES[0] else
        'Complete fitted field; source fitting mode: ' + settings.fit_mode, font=_font(17), fill='#404040')
    for index, (title, image, meta) in enumerate(zip(PANEL_TITLES, views['images'], views['metadata'])):
        col, row = index % 3, index // 3
        x, y = 40 + col*565, 153 + row*470
        draw.text((x, y), title, font=_font(23), fill='black')
        # Fit the entire content region, with the same physical scale across
        # all six rasters. Unused page area stays white; there is no black matte.
        scale = min(540/width_nm, 330/height_nm)
        size = (max(1, round(scale*width_nm)), max(1, round(scale*height_nm)))
        bounds = (region[0]*image.width, region[1]*image.height,
                  region[2]*image.width, region[3]*image.height)
        preview = image.resize(size,
            Image.Resampling.NEAREST if index == 0 else Image.Resampling.LANCZOS, box=bounds)
        if views.get('grid_design'):
            draw_design_grid(preview, info, region, views['grid_design'])
        left, top = x+(540-preview.width)//2, y+36+(330-preview.height)//2
        page.paste(preview, (left, top))
        bar = 20*scale
        # Put the scale on the white page, outside the image content.
        draw.line((x+12, y+399, x+12+bar, y+399), fill='black', width=3)
        draw.text((x+20+bar, y+385), '20 nm', font=_font(18), fill='black')
        if meta:
            text = 'Clipped {:.1%} | Area exposure {:g}x'.format(meta['clipped_pixel_fraction'], meta['canvas_area_gain'])
            draw.text((x, y+419), text, font=_font(18), fill='#303030')
        if index == 5:
            draw.text((x, y+443), 'N={} per design | seed {}'.format(settings.alignment.origami_per_design, settings.example_seed), font=_font(18), fill='#303030')
    metrics = views['optimization']
    footer = ('Optimization OFF: direct replacement assignment.' if metrics is None else
              'Optimized RMSE {:.5f} -> {:.5f}'.format(metrics['baseline_rmse'], metrics['optimized_rmse']))
    if metrics and 'validation_optimized' in metrics:
        footer += ' | Held-out RMSE {:.5f} -> {:.5f}'.format(metrics['validation_baseline']['expected_mse']**.5, metrics['validation_optimized']['expected_mse']**.5)
    footer += ' | Regularization {:g}'.format(settings.regularization_strength)
    if views.get('grid_design'):
        draw.text((40, 1078), 'White outlines: 48 nominal pixels of representative design ' +
                  views['grid_design']['name'], font=_font(18), fill='#404040')
    draw.text((40, 1110), footer, font=_font(20), fill='#202020')
    note = ('Alignment panels use entered errors; main prediction uses ideal alignment.' if not settings.alignment_enabled else
            'Expected alignment is a sampled mean; finite count shows one realization of alignment error.')
    draw.text((40, 1145), note, font=_font(20), fill='#202020')
    draw.text((40, 1190), '{} | Full fitted image | View page {} / {}'.format(info['encoding_scheme'], page_number, total), font=_font(18), fill='#404040')
    return page


def export_report(destination, settings, progress=lambda done, total, phase: None, cancelled=lambda: False, schemes=None):
    """Publish only a complete PDF; stream pages and discard large models per job."""
    settings.validate()
    keys = tuple(SELECTABLE_SCHEMES if schemes is None else schemes)
    phases = ['Binning image', 'Building alignment model', 'Rendering before optimization']
    if settings.optimize:
        phases.append('Optimizing replacement percentages')
        if settings.alignment_enabled:
            phases.append('Validating alignment optimization')
    phases += ['Rendering ideal alignment', 'Rendering expected alignment',
               'Rendering finite-count example', 'Writing PDF page']
    destination = Path(destination)
    def checkpoint(done, phase):
        if cancelled():
            raise ExportCancelled()
        progress(done, len(keys), phase)
    checkpoint(0, 'Preparing report')
    # Same filesystem for atomic replacement, preserving an existing file on error/cancel.
    with tempfile.TemporaryDirectory(prefix='.rgb-pdf-', dir=destination.parent) as folder:
        folder = Path(folder)
        source = folder/('source' + Path(settings.image_path).suffix)
        shutil.copyfile(settings.image_path, source)
        snapshot = replace(deepcopy(settings), image_path=str(source), multipliers=dict(settings.multipliers))
        jobs = report_jobs(snapshot.image_path, keys, snapshot.active_image_columns)
        output = folder/'report.pdf'
        cover_page(settings, len(jobs)).save(output, 'PDF', resolution=150, title='Exhaustive RGB encoding report')
        for index, (key, rotation) in enumerate(jobs):
            label = '{} | {} degrees'.format(SELECTABLE_SCHEMES[key].label, rotation)
            views = generate_views(snapshot, key, rotation,
                lambda phase, done=index, label=label: checkpoint(done + phases.index(phase)/len(phases), label + ' | ' + phase))
            checkpoint(index + (len(phases)-1)/len(phases), label + ' | Writing PDF page')
            page = view_page(snapshot, views, index+1, len(jobs))
            page.save(output, 'PDF', append=True, resolution=150)
            del page, views
            checkpoint(index+1, label + ' | Complete')
        checkpoint(len(jobs), 'Finishing PDF')
        os.replace(output, destination)
    return len(jobs)+1


def start_export_dialog(owner, destination, settings, exporter=None, button=None, graph=False, robustness=False):
    """Keep Tk on its main thread while a worker computes and writes the PDF."""
    import queue
    import threading
    import tkinter as tk
    from tkinter import messagebox, ttk
    exporter = export_report if exporter is None else exporter
    button = owner.exhaustive_pdf_button if button is None else button
    idle_text = 'Export similarity graph…' if graph else 'Export exhaustive PDF…'
    active_text = 'Show similarity progress…' if graph else 'Show PDF export progress…'
    noun = 'similarity graph' if graph else 'exhaustive PDF'
    if robustness:
        idle_text = 'Run similarity optimization'
        active_text = 'Show optimization progress…'
        noun = 'similarity optimization'
    messages = queue.Queue()
    cancellation = threading.Event()
    dialog = tk.Toplevel(owner.winfo_toplevel())
    dialog.title('Export ' + noun)
    dialog.geometry('690x240')
    dialog.minsize(690, 240)
    dialog.transient(owner.winfo_toplevel())
    total_views = len(SELECTABLE_SCHEMES)
    completion = tk.StringVar(value='0.0% · 0 / {} views complete'.format(total_views))
    ttk.Label(dialog, name='overall_status', textvariable=completion,
              font=('TkDefaultFont', 12, 'bold')).pack(fill='x', padx=15, pady=(15, 8))
    progress = ttk.Progressbar(dialog, name='overall_progress', mode='determinate', maximum=total_views)
    progress.pack(fill='x', padx=15)
    status = tk.StringVar(value='Preparing report…')
    ttk.Label(dialog, textvariable=status, wraplength=650).pack(fill='x', padx=15, pady=12)
    def cancel():
        cancellation.set()
        status.set('Cancelling after the current calculation…')
        cancel_button.configure(state='disabled')
    cancel_button = ttk.Button(dialog, text='Cancel', command=cancel)
    cancel_button.pack(pady=12)
    dialog.protocol('WM_DELETE_WINDOW', cancel)
    button.configure(state='normal', text=active_text)
    def worker():
        try:
            pages = exporter(destination, settings,
                progress=lambda done, total, phase: messages.put(('progress', done, total, phase)),
                cancelled=cancellation.is_set)
            messages.put(('done', pages))
        except ExportCancelled:
            messages.put(('cancelled',))
        except Exception as exc:
            messages.put(('error', str(exc)))
    def poll():
        try:
            while True:
                item = messages.get_nowait()
                if item[0] == 'progress':
                    _, done, total, phase = item
                    progress.configure(value=done, maximum=total)
                    if not cancellation.is_set():
                        completion.set('{:.1%} · {} / {} views complete'.format(done/total, int(done), total))
                        status.set(phase)
                else:
                    button.configure(state='normal', text=idle_text)
                    dialog.destroy()
                    if hasattr(owner, 'status'):
                        owner.status.set(('Saved ' + noun + ': ' + str(destination)) if item[0] == 'done' else
                                         ('Export failed: ' + item[1]) if item[0] == 'error' else 'Export cancelled.')
                    if item[0] == 'done':
                        messagebox.showinfo('Graph exported' if graph else 'PDF exported',
                            (('Winner: ' + item[1]['winner']['label'] + '\nWorst-case RMSE: {:.5f}\n\n'.format(item[1]['winner']['worst_rmse']) if robustness else '') +
                             'Saved results:\n' + '\n'.join(item[1]['paths'])) if graph else
                            'Saved {} pages to:\n{}'.format(item[1], destination), parent=owner)
                    elif item[0] == 'error':
                        messagebox.showerror('Cannot export ' + noun, item[1], parent=owner)
                    return
        except queue.Empty:
            pass
        if dialog.winfo_exists():
            dialog.after(100, poll)
    try:
        dialog.update_idletasks()
        dialog.deiconify()
        dialog.lift()
        # Register polling before starting work; if setup fails no orphan worker
        # or permanently disabled/relabelled export button is left behind.
        poll_id = dialog.after(100, poll)
        threading.Thread(target=worker, name='rgb-exhaustive-pdf', daemon=True).start()
    except Exception:
        if 'poll_id' in locals():
            dialog.after_cancel(poll_id)
        dialog.destroy()
        button.configure(state='normal', text=idle_text)
        raise
    return dialog
