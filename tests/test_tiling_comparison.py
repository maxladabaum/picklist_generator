import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from image_encoding import TILING_SCHEMES, SWEEP_SCHEMES, JOINT_TILING_SCHEMES, COLOR_BITS, CHANNELS, compile_designs, bin_image, geometry, placement, physical_reference_raster, physical_raster_size
from color_light import canvas_area_gain
from gaussian_reconstruction import render_gaussian_overlay
from gaussian_optimization import optimize_designs
from alignment_reconstruction import AlignmentModel, AlignmentSettings, transformed_sites
from tiling_comparison import FAMILY_FILTERS, LAYOUT_FILTERS, parse_widths, selected_layouts, TilingComparison
import test_image_encoding


class TilingGeometryTests(unittest.TestCase):
    def test_all_allocations_preserve_budget_and_physical_origami(self):
        self.assertEqual(len(TILING_SCHEMES), 35)
        self.assertEqual(len(selected_layouts(LAYOUT_FILTERS[0])), 16)
        for key, scheme in TILING_SCHEMES.items():
            with self.subTest(key=key):
                info = geometry(key)
                self.assertEqual(scheme.design_count, 48)
                self.assertEqual(len({entry['code'] for entry in info['codebook']}), 48)
                self.assertTrue(all(len(entry['bits']) == 6 for entry in info['codebook']))
                self.assertEqual(info['columns'] * info['rows'], 768)
                self.assertAlmostEqual(info['field_width_nm'], 59.4*scheme.tile_columns)
                self.assertAlmostEqual(info['field_height_nm'], 40.8*scheme.tile_rows)
                self.assertEqual(canvas_area_gain(key, True), scheme.tile_count)
                self.assertEqual(canvas_area_gain(key, False), 1)
                pixels = [[[93, 147, 201] for _ in range(info['columns'])] for _ in range(info['rows'])]
                designs = compile_designs(pixels, 75, {c: 2 for c in CHANNELS}, key)
                for channel in range(3):
                    sites = [site for design in designs if design['channel'] == channel for site in design['image_sites']]
                    self.assertEqual(len({(s['pixel_x'], s['pixel_y']) for s in sites}), 768)
                for design in designs:
                    sites = design['image_sites']
                    self.assertAlmostEqual(sites[5]['x_nm']-sites[0]['x_nm'], 5*9.9)
                    self.assertAlmostEqual(sites[-1]['y_nm']-sites[0]['y_nm'], 7*5.1)
                    if scheme.tiled:
                        position = placement(scheme, design['x_shift'], design['y_shift'])
                        self.assertEqual(position['quadrant_normalization_weight'], 1)

    def test_extreme_factorizations_have_bounded_resolved_rasters(self):
        for key, scheme in JOINT_TILING_SCHEMES.items():
            width, height = physical_raster_size(key)
            self.assertLessEqual(width*height,12_000_000,key)
            self.assertAlmostEqual(width/height,59.4*scheme.tile_columns/(40.8*scheme.tile_rows))
            self.assertGreaterEqual(width/(6*scheme.tile_columns*max(scheme.x_counts)),1)
            self.assertGreaterEqual(height/(8*scheme.tile_rows*max(scheme.y_counts)),1)

    def test_joint_sweeps_cover_every_partition_with_unique_codes_and_pixels(self):
        self.assertEqual(len(JOINT_TILING_SCHEMES), 392)
        for choice, parent in FAMILY_FILTERS.items():
            layouts = selected_layouts(choice)
            first = layouts[0][1]
            budget = first.x_states * first.y_states
            self.assertEqual({(s.x_states,s.y_states,s.tile_columns,s.tile_rows) for _,s in layouts},
                             {(x,budget//x,tx,ty) for x in range(1,budget+1) if budget%x == 0
                              for tx in range(1,x+1) for ty in range(1,budget//x+1)})
            for key, scheme in layouts:
                info = geometry(key)
                self.assertEqual(scheme.parent_scheme, parent)
                self.assertLessEqual(scheme.design_count, 64)
                self.assertEqual(len({v['code'] for v in info['codebook']}), scheme.design_count)
                self.assertTrue(all(len(v['bits']) == 6 for v in info['codebook']))
                self.assertEqual(sum(scheme.x_counts), scheme.x_states)
                self.assertEqual(sum(scheme.y_counts), scheme.y_states)
                self.assertGreater(min(scheme.x_counts + scheme.y_counts), 0)
                pixels = [[[100,140,180]]*info['columns'] for _ in range(info['rows'])]
                designs = compile_designs(pixels,50,{c:5 for c in CHANNELS},key)
                for channel in range(3):
                    sites = [site for d in designs if d['channel']==channel for site in d['image_sites']]
                    self.assertEqual(len(sites),info['columns']*info['rows'])
                    self.assertEqual(len({(s['pixel_x'],s['pixel_y']) for s in sites}),len(sites))
                for d in designs:
                    sites = d['image_sites']
                    self.assertAlmostEqual(sites[5]['x_nm']-sites[0]['x_nm'],49.5)
                    self.assertAlmostEqual(sites[-1]['y_nm']-sites[0]['y_nm'],35.7)

    def test_existing_four_quadrant_and_single_field_predictions_are_preserved(self):
        for new, old in (('tiling_0_0_2_2', 'fixed_4x4'), ('tiling_1_1_1_1', 'quadrants_2x2'),
                         ('sweep_joint_7x3_tiles_2x2','quadrants_joint_7x3'),
                         ('sweep_joint_6x3_tiles_1x1','joint_6x3'),
                         ('sweep_joint_5x4_tiles_2x2','quadrants_joint_5x4')):
            info = geometry(new)
            pixels = [[[128, 190, 40] for _ in range(info["columns"])] for _ in range(info["rows"])]
            results = []
            for key in (new, old):
                designs = compile_designs(pixels, 50, {c: 5 for c in CHANNELS}, key, linear_light=True)
                results.append(render_gaussian_overlay(designs, 7.5, linear_light=True, normalize_designs=True, compensate_area=True))
            np.testing.assert_array_equal(results[0].rgb, results[1].rgb)
            np.testing.assert_allclose(results[0].density, results[1].density)

    def test_all_layouts_bin_upright_image_and_preserve_physical_aspect(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'source.png'
            image = Image.new('RGB', (80, 60), (40, 90, 180))
            image.paste((220, 140, 30), (0, 0, 40, 30))
            image.save(path)
            for key in SWEEP_SCHEMES:
                for rotation in (0, 90):
                    pixels = bin_image(path, 'Stretch', rotation, key, linear_light=True)
                    raster = physical_reference_raster(Image.fromarray(np.asarray(pixels, dtype='uint8')), key, rotation)
                    info = geometry(key, rotation)
                    self.assertAlmostEqual(raster.width/raster.height, info['display_field_width_nm']/info['display_field_height_nm'])
                    # The distinctive upper-left patch stays upright after rendering.
                    self.assertGreater(raster.getpixel((raster.width//8, raster.height//8))[0], 180)
                    self.assertLess(raster.getpixel((7*raster.width//8, 7*raster.height//8))[0], 70)

    def test_sixteen_tiles_optimize_and_align_without_moving_tile_offsets(self):
        key = 'tiling_2_2_0_0'
        pixels = [[[128, 100, 50] for _ in range(24)] for _ in range(32)]
        designs = compile_designs(pixels, 50, {c: 5 for c in CHANNELS}, key, linear_light=True)
        options = dict(linear_light=True, normalize_designs=True, compensate_area=True)
        overlay = render_gaussian_overlay(designs, 7.5, **options)
        target = np.full(np.asarray(overlay.rgb).shape, .15)
        optimized, metrics = optimize_designs(designs, target, 7.5, **options)
        self.assertLessEqual(metrics['optimized_rmse'], metrics['baseline_rmse'] + 1e-12)
        self.assertEqual([d['bits'] for d in optimized], [d['bits'] for d in designs])
        model = AlignmentModel(designs, AlignmentSettings(), 5, **options)
        expected = model.expected(designs)
        self.assertEqual(expected.metadata['canvas_area_gain'], 16)
        self.assertEqual(expected.metadata['fwhm_nm'], 5)
        first = next(d for d in designs if d['quadrant'] == 0)
        last = next(d for d in designs if d['quadrant'] == 15)
        a = transformed_sites(first, model.settings, (1, 2, 3))
        b = transformed_sites(last, model.settings, (1, 2, 3))
        np.testing.assert_allclose(b-a, np.tile([3*59.4, 3*40.8], (48, 1)))

    def test_width_validation_and_layout_filters(self):
        self.assertEqual(parse_widths('5, 7.5, 10, 5'), [5, 7.5, 10])
        for invalid in ('', '0', '-1', 'nan', 'inf', 'a', '1,2,3,4,5,6,7'):
            with self.assertRaises(ValueError):
                parse_widths(invalid)
        choices = [choice for choice in LAYOUT_FILTERS[2:] if choice not in FAMILY_FILTERS]
        groups = [selected_layouts(choice) for choice in choices]
        self.assertEqual(sum(map(len, groups)), len(SWEEP_SCHEMES))
        for choice, group in zip(choices, groups):
            self.assertTrue(all(s.tile_count == int(choice.split()[0]) for _, s in group))


class TilingWorkflowTests(unittest.TestCase):
    setUp = test_image_encoding.ImageWorkflowTests.setUp

    def test_matrix_honors_settings_and_does_not_change_main_designs(self):
        view = self.view
        view.gaussian_exposure.set('0.7')
        view.image_path.set(str(self.image_path))
        view._load()
        original_designs = view.designs
        original_width = view.gaussian_fwhm.get()
        window = TilingComparison(view)
        view.tiling_window = window
        window.withdraw()
        window.widths.set('5, 10')
        window.refresh()
        self.assertFalse(window.stale, window.status.get())
        self.assertEqual(len(window.results), 32, window.status.get())
        for (key, width), result in window.results.items():
            metadata = result['metadata']
            self.assertEqual(metadata['fwhm_nm'], width)
            self.assertEqual(metadata['exposure'], .7)
            self.assertTrue(metadata['linear_light'])
            self.assertTrue(metadata['normalize_designs'])
            self.assertTrue(metadata['compensate_area'])
            self.assertEqual(metadata['canvas_area_gain'], geometry(key)['tile_count'])
        self.assertIs(view.designs, original_designs)
        self.assertEqual(view.gaussian_fwhm.get(), original_width)
        # Changes invalidate displayed results rather than silently leaving old images.
        view.gaussian_exposure.set('0.8')
        self.assertTrue(window.stale)
        self.assertEqual(window.results, {})
        window.widths.set('nan')
        window.refresh()
        self.assertIn('Cannot compare', window.status.get())
        self.assertEqual(window.results, {})
        window.layout_filter.set(LAYOUT_FILTERS[1])
        window.widths.set('7.5')
        window.refresh()
        self.assertFalse(window.stale, window.status.get())
        self.assertEqual(len(window.results), len(SWEEP_SCHEMES), window.status.get())

    def test_all_layouts_select_and_inspect_then_export_sixteen_tiles(self):
        view = self.view
        view.image_path.set(str(self.image_path))
        view._load()
        choices = view.scheme_picker["values"]
        with patch("rgb_image_view.messagebox.showerror") as error:
            for key, scheme in SWEEP_SCHEMES.items():
                with self.subTest(key=key):
                    self.assertIn(scheme.label, choices)
                    view.scheme.set(scheme.label)
                    self.assertEqual(view._scheme_id(), key)
                    self.assertEqual(self.app.config["image_encoding_scheme"], key)
                    self.assertEqual(len(view.designs), scheme.design_count, view.status.get())
                    view.design_name.set(view.designs[-1]["name"])
                    view._draw_design()
                    self.assertEqual(len(view.pixel_canvases[0].find_withtag("design_pixels")), 96)
                    self.assertLessEqual(float(view.pixel_canvases[0]['height']), 302)
            key = 'tiling_2_2_0_0'
            window = TilingComparison(view)
            view.tiling_window = window
            window.withdraw()
            window.use_layout(key)
            self.assertEqual(view._scheme_id(), key)
            self.assertIn('16 tiles', view.explanation.get())
            view._prepare()
            self.assertEqual(len(view.runs), 48)
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            manifest_path = next(self.folder.glob('*/image_manifest.json'))
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest['encoding_scheme'], key)
            self.assertEqual(manifest['geometry']['tile_count'], 16)
            self.assertEqual(manifest['gaussian_reconstruction']['canvas_area_gain'], 16)
            self.assertEqual({d['quadrant'] for d in manifest['designs']}, set(range(16)))
            view._store()
            error.assert_not_called()

    def test_original_budget_sweep_preserves_color_bits_and_renders_every_layout(self):
        view = self.view
        view.image_path.set(str(self.image_path))
        view._load()
        window = TilingComparison(view)
        window.withdraw()
        window.widths.set('7.5')
        window.layout_filter.set('16 positions / 48 types (80 layouts)')
        window.refresh()
        self.assertEqual(len(window.results),80,window.status.get())
        for key, _ in window.results:
            info = geometry(key)
            self.assertEqual(info['design_count'],48)
            for entry in info['codebook']:
                self.assertEqual(entry['bits'][:2],list(map(int,COLOR_BITS[entry['color']])))
            self.assertEqual(info['encoding_mode'],'fixed_color_position_codebook')
        key = 'sweep_fixed_4x4_tiles_3x3'
        window.use_layout(key)
        self.assertEqual(view._scheme_id(),key)
        self.assertEqual(len(view.designs),48,view.status.get())
        self.assertIn('4 bits jointly',view.explanation.get())

    def test_joint_family_optimizes_every_layout_and_width(self):
        view = self.view
        view.image_path.set(str(self.image_path))
        view._load()
        window = TilingComparison(view)
        window.withdraw()
        window.widths.set('5, 10')
        window.layout_filter.set(next(k for k,v in FAMILY_FILTERS.items() if v == 'joint_7x3'))
        view.optimize_enabled.set(True)
        def optimized(designs, fwhm_nm=None):
            return designs, dict(baseline_rmse=.2, optimized_rmse=.1)
        with patch.object(view, '_optimize', side_effect=optimized) as optimize:
            window.refresh()
        self.assertEqual(len(window.results), 168, window.status.get())
        self.assertEqual(optimize.call_count, 168)
        self.assertEqual({(c.args[0][0]['encoding_scheme'], c.kwargs['fwhm_nm'])
                          for c in optimize.call_args_list}, set(window.results))
        self.assertTrue(all(len(c.args[0]) == 63 for c in optimize.call_args_list))

    def test_optimization_called_independently_for_every_width_and_layout(self):
        view = self.view
        view.image_path.set(str(self.image_path))
        view._load()
        view.optimize_enabled.set(True)
        window = TilingComparison(view)
        window.withdraw()
        window.widths.set('5, 10')
        window.layout_filter.set('16 tiles')
        def optimized(designs, fwhm_nm=None):
            return designs, dict(baseline_rmse=.2, optimized_rmse=.1)
        with patch.object(view, '_optimize', side_effect=optimized) as optimize:
            window.refresh()
        self.assertEqual(len(window.results), 2*len(selected_layouts("16 tiles")), window.status.get())
        self.assertEqual(optimize.call_count, len(window.results))
        self.assertEqual({(call.args[0][0]['encoding_scheme'], call.kwargs['fwhm_nm']) for call in optimize.call_args_list}, set(window.results))
        self.assertTrue(all(result['optimization']['optimized_rmse'] == .1 for result in window.results.values()))
