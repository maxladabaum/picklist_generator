import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, PdfParser

from exhaustive_pdf import ReportSettings, PANEL_TITLES, ROTATIONS, ExportCancelled, export_report, generate_views, report_jobs, view_page, best_rotation, display_region
from image_encoding import CHANNELS, SELECTABLE_SCHEMES, bin_image, compile_designs, geometry
from gaussian_reconstruction import render_gaussian_overlay
from alignment_reconstruction import AlignmentSettings
import test_image_encoding


class PDFReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.image = self.folder/'image.png'
        source = Image.new('RGB', (80, 60), (60, 140, 190))
        source.paste((230, 80, 20), (0, 0, 30, 25))
        source.save(self.image)
        self.settings = ReportSettings(str(self.image), 'Center crop', 50, {c: 5 for c in CHANNELS},
            7.5, .8, alignment=AlignmentSettings(origami_per_design=1), example_seed=123)

    def test_every_dropdown_entry_uses_the_best_rotation(self):
        jobs = report_jobs(self.image)
        self.assertEqual(len(jobs), len(SELECTABLE_SCHEMES))
        self.assertEqual(set(key for key, _ in jobs), set(SELECTABLE_SCHEMES))
        for size in ((80,60), (60,80), (100,100), (1000,30)):
            for key in SELECTABLE_SCHEMES:
                chosen = best_rotation(size, key)
                def padding(angle):
                    info = geometry(key, angle)
                    ratio = info['display_field_width_nm']/info['display_field_height_nm']/(size[0]/size[1])
                    return 1-min(ratio,1/ratio)
                self.assertLessEqual(padding(chosen), padding(90-chosen)+1e-12)
        self.assertEqual(best_rotation((80,60), 'fixed_4x4'), 0)
        self.assertEqual(best_rotation((60,80), 'fixed_4x4'), 90)
        self.assertEqual(best_rotation((100,100), 'fixed_4x4'), 0)
        self.assertEqual(len(PANEL_TITLES), 6)

    def test_rotation_respects_source_exif_orientation(self):
        path = self.folder/'oriented.jpg'
        source = Image.new('RGB', (80, 60), 'red')
        exif = source.getexif()
        exif[274] = 6  # Displayed image is portrait.
        source.save(path, exif=exif)
        self.assertEqual(report_jobs(path, ['fixed_4x4']), [('fixed_4x4', 90)])

    def test_real_panels_match_direct_prediction_and_alignment_settings(self):
        for alignment in (False, True):
            settings = replace(self.settings, alignment_enabled=alignment)
            views = generate_views(settings, 'tiling_2_2_0_0', 90)
            self.assertEqual(len(views['images']), 6)
            self.assertIsNone(views['optimization'])
            self.assertEqual(views['geometry']['origami_rotation_degrees_clockwise'], 90)
            self.assertEqual(views['geometry']['tile_count'], 16)
            np.testing.assert_array_equal(views['images'][1], views['images'][2])
            np.testing.assert_array_equal(views['images'][2], views['images'][4 if alignment else 3])
            self.assertEqual(views['metadata'][4]['alignment']['origami_per_design'], 1)
            self.assertEqual(views['metadata'][5]['example_seed'], 123)
            pixels = bin_image(self.image, settings.fit_mode, 90, 'tiling_2_2_0_0', linear_light=True)
            designs = compile_designs(pixels, 50, settings.multipliers, 'tiling_2_2_0_0', 90, linear_light=True)
            ideal = render_gaussian_overlay(designs, 7.5, .8, **settings.light_options())
            np.testing.assert_array_equal(views['images'][3], ideal.rgb)
            self.assertTrue(any(not np.array_equal(views['images'][3], image) for image in views['images'][4:]))

    def test_optimized_and_robust_optimized_panels(self):
        for alignment in (False, True):
            settings = replace(self.settings, optimize=True, alignment_enabled=alignment)
            views = generate_views(settings, 'fixed_4x4', 0)
            metrics = views['optimization']
            self.assertIsNotNone(metrics)
            self.assertLessEqual(metrics['optimized_rmse'], metrics['baseline_rmse']+1e-12)
            if alignment:
                self.assertIn('validation_optimized', metrics)
            self.assertEqual(views['metadata'][2]['fwhm_nm'], 7.5)
            self.assertEqual(views['metadata'][2]['exposure'], .8)

    def test_joint_family_endpoints_render_optimize_and_score(self):
        settings = replace(self.settings, optimize=True, alignment_enabled=True)
        for key, count in (('sweep_joint_6x3_tiles_6x3',54),
                           ('sweep_joint_7x3_tiles_7x3',63),
                           ('sweep_joint_5x4_tiles_5x4',60)):
            views = generate_views(settings,key,90)
            self.assertEqual(len(views['images']),6)
            self.assertEqual(views['geometry']['design_count'],count)
            self.assertIn('validation_optimized',views['optimization'])
            self.assertEqual(views['metadata'][3]['canvas_area_gain'],count//3)
            self.assertLessEqual(views['metadata'][3]['raster_pixel_x_nm'],settings.fwhm_nm/4)
            score = generate_views(settings,key,90,score_only=True)
            self.assertEqual(score['design_count'],count)
            self.assertTrue(np.isfinite(score['final_rmse']))

    def test_real_two_page_pdf_is_readable_and_cancel_preserves_existing_file(self):
        destination = self.folder/'report.pdf'
        progress = []
        pages = export_report(destination, self.settings, schemes=['fixed_4x4'],
                              progress=lambda done, total, phase: progress.append((done,total,phase)))
        self.assertEqual(pages, 2)
        with PdfParser.PdfParser(filename=str(destination)) as pdf:
            self.assertEqual(len(pdf.pages), 2)
        self.assertEqual(progress[-1][:2], (1,1))
        original = destination.read_bytes()
        cancelled = False
        def update(done, total, phase):
            nonlocal cancelled
            if done == 1:
                cancelled = True
        with self.assertRaises(ExportCancelled):
            export_report(destination, self.settings, schemes=['fixed_4x4'], progress=update, cancelled=lambda: cancelled)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(list(self.folder.glob('.rgb-pdf-*')), [])
        with patch('exhaustive_pdf.generate_views', side_effect=ValueError('model failure')):
            with self.assertRaisesRegex(ValueError, 'model failure'):
                export_report(destination, self.settings, schemes=['fixed_4x4'])
        self.assertEqual(destination.read_bytes(), original)

    def test_all_six_panels_have_same_physical_scale_despite_raster_resolution(self):
        views = dict(geometry=geometry('tiling_2_2_0_0', 90), optimization=None,
                     images=[Image.new('RGB', (size, size), 'red') for size in (10, 20, 40, 100, 300, 500)],
                     metadata=[None]*6)
        page = np.asarray(view_page(self.settings, views, 1, 46))
        areas = []
        for index in range(6):
            x, y = 40+(index%3)*565, 153+(index//3)*470
            crop = page[y+36:y+366, x:x+540]
            self.assertFalse(np.any(np.all(crop == [21,25,29], axis=2)))
            areas.append(int(np.sum(np.all(crop == [255, 0, 0], axis=2))))
        self.assertGreater(areas[0], 50000)
        self.assertEqual(len(set(areas)), 1)

    def test_fit_padding_is_trimmed_geometrically_without_losing_real_black_content(self):
        from PIL import ImageOps
        settings = replace(self.settings, fit_mode='Fit with black padding')
        info = geometry('fixed_4x4', 90)
        source = Image.new('RGB', (80,60), 'red')
        source.paste('green', (40,0,80,30))
        source.paste('blue', (0,30,40,60))
        source.paste('yellow', (40,30,80,60))
        source.paste('black', (0,0,12,60))  # Real dark content at the source edge.
        source.save(self.image)
        region, physical = display_region(settings, info)
        self.assertAlmostEqual(physical[0]/physical[1], 80/60)
        self.assertEqual(region[0], 0)
        self.assertGreater(region[1], 0)
        images = [ImageOps.pad(source, (408,594), color='black')]*6
        page = np.asarray(view_page(settings, dict(geometry=info, optimization=None, images=images, metadata=[None]*6), 1,46))
        # Source aspect is 4:3, so the full visible image is 440 x 330 pixels.
        for index in range(6):
            x,y = 40+(index%3)*565+50,153+(index//3)*470+36
            self.assertTrue(np.array_equal(page[y+15,x+15], [0,0,0]))
            self.assertTrue(np.array_equal(page[y+15,x+100], [255,0,0]))
            self.assertTrue(np.array_equal(page[y+15,x+425], [0,128,0]))
            self.assertTrue(np.array_equal(page[y+315,x+100], [0,0,255]))
            self.assertTrue(np.array_equal(page[y+315,x+425], [255,255,0]))

    def test_design_grid_matches_all_panels_without_changing_prediction_rasters(self):
        settings = replace(self.settings, fit_mode='Fit with black padding')
        for scheme in ('fixed_4x4', 'quadrants_joint_7x3'):
            for rotation in (0, 90):
                info = geometry(scheme, rotation)
                pixels = bin_image(self.image, settings.fit_mode, rotation, scheme)
                design = compile_designs(pixels, 50, settings.multipliers, scheme, rotation)[0]
                self.assertEqual(len(design['image_sites']), 48)
                images = [Image.new('RGB', (300, 300), 'red') for _ in range(6)]
                views = dict(geometry=info, optimization=None, images=images,
                             metadata=[None]*6, grid_design=design)
                page = np.asarray(view_page(settings, views, 1, 46))
                reference = None
                for index in range(6):
                    x, y = 40+(index%3)*565, 153+(index//3)*470
                    crop = page[y+36:y+366, x:x+540]
                    if reference is None:
                        reference = crop
                        self.assertTrue(np.any(np.all(crop == [0, 0, 0], axis=2)))
                    else:
                        np.testing.assert_array_equal(crop, reference)
                for image in images:
                    self.assertEqual(image.getextrema(), ((255,255),(0,0),(0,0)))

    def test_exhaustive_pdf_contains_all_selectable_view_pages(self):
        # Rendering is tested above; exercise the complete writer/scheduler cheaply.
        tiny_page = Image.new('RGB', (200, 100), 'white')
        destination = self.folder/'complete.pdf'
        with patch('exhaustive_pdf.generate_views', return_value={}) as views, \
             patch('exhaustive_pdf.view_page', return_value=tiny_page), \
             patch('exhaustive_pdf.cover_page', return_value=tiny_page):
            count = export_report(destination, self.settings)
        self.assertEqual(count, len(SELECTABLE_SCHEMES)+1)
        self.assertEqual([(call.args[1],call.args[2]) for call in views.call_args_list], report_jobs(self.image))
        with PdfParser.PdfParser(filename=str(destination)) as pdf:
            self.assertEqual(len(pdf.pages), len(SELECTABLE_SCHEMES)+1)


class PDFWorkflowTests(unittest.TestCase):
    setUp = test_image_encoding.ImageWorkflowTests.setUp

    def test_export_button_snapshots_current_settings_without_changing_active_design(self):
        view = self.view
        view.image_path.set(str(self.image_path))
        view._load()
        view.rotation.set('90')
        view._load()
        view.regularization.set('0.07')
        designs = view.designs
        destination = str(self.folder/'report.pdf')
        with patch('rgb_image_view.filedialog.asksaveasfilename', return_value=destination), \
             patch('exhaustive_pdf.start_export_dialog') as start:
            view.exhaustive_pdf_button.invoke()
        start.assert_called_once()
        owner, path, settings = start.call_args.args
        self.assertIs(owner, view)
        self.assertEqual(path, destination)
        self.assertEqual(settings.fwhm_nm, float(view.gaussian_fwhm.get()))
        self.assertEqual(settings.light_options(), view._light_options())
        self.assertEqual(settings.alignment, view._alignment_settings())
        self.assertEqual(settings.example_seed, view.example_seed)
        self.assertEqual(settings.regularization_strength, .07)
        self.assertIs(view.designs, designs)
        self.assertEqual(view.rotation.get(), '90')
        self.assertEqual(view.runs, [])

    def test_startup_import_failure_is_reported_instead_of_silent_tk_exception(self):
        import builtins
        original_import = builtins.__import__
        def import_with_failure(name, *args, **kwargs):
            if name == 'exhaustive_pdf':
                raise ImportError('cannot import name best_rotation from image_encoding')
            return original_import(name, *args, **kwargs)
        view = self.view
        view.image_path.set(str(self.image_path))
        view._load()
        with patch('builtins.__import__', side_effect=import_with_failure), \
             patch('rgb_image_view.messagebox.showerror') as error, \
             patch('rgb_image_view.filedialog.asksaveasfilename') as save:
            view.exhaustive_pdf_button.invoke()
        error.assert_called_once()
        self.assertIn('Restart the app', error.call_args.args[1])
        self.assertIn('Cannot start PDF export', view.status.get())
        save.assert_not_called()
        self.assertEqual(str(view.exhaustive_pdf_button['state']), 'normal')

    def test_hidden_export_window_can_be_reopened_without_starting_another_export(self):
        import tkinter as tk
        view = self.view
        window = tk.Toplevel(self.app)
        window.withdraw()
        view.pdf_export_window = window
        with patch('rgb_image_view.filedialog.asksaveasfilename') as save, \
             patch.object(window, 'deiconify') as reveal, patch.object(window, 'lift') as lift:
            view.exhaustive_pdf_button.invoke()
        reveal.assert_called_once()
        lift.assert_called_once()
        save.assert_not_called()
        window.destroy()

    def test_save_dialog_and_worker_start_failures_are_visible_and_retryable(self):
        view = self.view
        view.image_path.set(str(self.image_path))
        view._load()
        with patch('rgb_image_view.filedialog.asksaveasfilename', side_effect=RuntimeError('save dialog failed')), \
             patch('rgb_image_view.messagebox.showerror') as error:
            view.exhaustive_pdf_button.invoke()
        self.assertIn('save dialog failed', error.call_args.args[1])
        with patch('rgb_image_view.filedialog.asksaveasfilename', return_value=str(self.folder/'report.pdf')) as save, \
             patch('exhaustive_pdf.start_export_dialog', side_effect=RuntimeError('worker start failed')), \
             patch('rgb_image_view.messagebox.showerror') as error:
            view.exhaustive_pdf_button.invoke()
        self.assertIs(save.call_args.kwargs['parent'], self.app)
        self.assertIn('worker start failed', error.call_args.args[1])
        self.assertEqual(str(view.exhaustive_pdf_button['state']), 'normal')
        self.assertEqual(str(view.exhaustive_pdf_button['text']), 'Export exhaustive PDF…')


class PDFDialogTests(unittest.TestCase):
    def test_export_worker_reports_completion_without_blocking_tk(self):
        import tkinter as tk
        from tkinter import ttk
        self.app = tk.Tk()
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self.view = ttk.Frame(self.app)
        self.view.exhaustive_pdf_button = ttk.Button(self.view)
        self.image_path = Path('/tmp/unused-test-image.png')
        self.folder = Path('/tmp')
        import threading
        import time
        from exhaustive_pdf import start_export_dialog
        settings = ReportSettings(str(self.image_path), 'Center crop', 50, {c:5 for c in CHANNELS}, 7.5, 1)
        worker_threads = []
        release = threading.Event()
        self.addCleanup(release.set)
        def write_report(destination, snapshot, progress, cancelled):
            worker_threads.append(threading.get_ident())
            progress(23.25, 46, 'Rendering expected alignment')
            release.wait(3)
            progress(46, 46, 'Complete')
            return 47
        with patch('exhaustive_pdf.export_report', side_effect=write_report), \
             patch('tkinter.messagebox.showinfo') as complete:
            dialog = start_export_dialog(self.view, str(self.folder/'report.pdf'), settings)
            self.assertEqual(str(self.view.exhaustive_pdf_button['state']), 'normal')
            self.assertEqual(str(self.view.exhaustive_pdf_button['text']), 'Show PDF export progress…')
            bar = dialog.nametowidget('overall_progress')
            deadline = time.monotonic()+2
            while float(bar['value']) != 23.25 and time.monotonic() < deadline:
                self.app.update()
                time.sleep(.01)
            self.assertEqual(float(bar['maximum']), 46)
            self.assertEqual(float(bar['value']), 23.25)
            label = dialog.nametowidget('overall_status')
            self.assertIn('50.5%', dialog.getvar(label['textvariable']))
            release.set()
            deadline = time.monotonic()+3
            while dialog.winfo_exists() and time.monotonic() < deadline:
                self.app.update()
                time.sleep(.01)
            self.assertFalse(dialog.winfo_exists())
            self.assertEqual(str(self.view.exhaustive_pdf_button['state']), 'normal')
            complete.assert_called_once()
            self.assertIn('47 pages', complete.call_args.args[1])
        self.assertNotEqual(worker_threads[0], threading.get_ident())
