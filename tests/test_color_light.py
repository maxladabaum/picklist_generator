import tempfile
from pathlib import Path
import unittest
import numpy as np
from PIL import Image
from color_light import srgb_to_linear, linear_to_srgb, fit_linear_source
from image_encoding import CHANNELS, ENCODING_SCHEMES, compile_designs, bin_image
from gaussian_reconstruction import render_gaussian_overlay
from gaussian_optimization import optimize_designs
from alignment_reconstruction import AlignmentModel, AlignmentSettings


class LinearLightTests(unittest.TestCase):
    def test_transfer_functions_and_linear_resampling(self):
        self.assertAlmostEqual(float(srgb_to_linear(128/255)),.21586050011389926)
        values = np.linspace(0,1,256)
        np.testing.assert_allclose(linear_to_srgb(srgb_to_linear(values)),values,atol=1e-14)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'pattern.png'
            image = Image.new('RGB',(2,1))
            image.putdata([(0,0,0),(255,255,255)])
            image.save(path)
            fitted = fit_linear_source(path,'Stretch',(2,1),(1,1))
            np.testing.assert_allclose(fitted,.5)
            self.assertAlmostEqual(float(linear_to_srgb(fitted)[0,0,0])*255,187.516,places=2)
            Image.new('RGBA',(10,10),(255,0,0,128)).save(path)
            np.testing.assert_allclose(fit_linear_source(path,'Stretch',(3,3))[:,:,0],128/255,atol=1e-7)
            pixels = bin_image(path,linear_light=True)
            self.assertEqual(pixels[16][12],[188,0,0])

    def test_all_scheme_brightness_is_comparable_without_auto_exposure(self):
        for key,scheme in ENCODING_SCHEMES.items():
            pixels = [[[128]*3 for _ in range(6*scheme.x_states)] for _ in range(8*scheme.y_states)]
            designs = compile_designs(pixels,25,{c:255 for c in CHANNELS},key,linear_light=True)
            image = render_gaussian_overlay(designs,linear_light=True,normalize_designs=True)
            middle = np.asarray(image.rgb)[image.rgb.height//2,image.rgb.width//2]
            np.testing.assert_allclose(middle,[66 if scheme.tiled else 128]*3,atol=1)
            self.assertAlmostEqual(image.metadata['design_abundance_scale'],48/scheme.design_count)
            if key == 'nine_joint_18x9':
                unnormalized = render_gaussian_overlay(designs,linear_light=True)
                np.testing.assert_array_equal(unnormalized.density,image.density)
                self.assertEqual(np.asarray(unnormalized.rgb)[unnormalized.rgb.height//2,unnormalized.rgb.width//2,0],255)

    def test_quadrant_area_compensation_matches_exposure_and_preserves_raw_signal(self):
        pixels = [[[128]*3 for _ in range(24)] for _ in range(32)]
        designs = compile_designs(pixels,25,{c:255 for c in CHANNELS},"quadrants_2x2",linear_light=True)
        normal = render_gaussian_overlay(designs,linear_light=True,normalize_designs=True)
        compensated = render_gaussian_overlay(designs,linear_light=True,normalize_designs=True,compensate_area=True)
        exposed = render_gaussian_overlay(designs,exposure=4,linear_light=True,normalize_designs=True)
        np.testing.assert_array_equal(normal.density,compensated.density)
        np.testing.assert_array_equal(np.asarray(compensated.rgb),np.asarray(exposed.rgb))
        center = np.asarray(compensated.rgb)[compensated.rgb.height//2,compensated.rgb.width//2]
        np.testing.assert_allclose(center,[128]*3,atol=1)
        self.assertEqual(compensated.metadata["canvas_area_gain"],4)
        original = compile_designs(pixels,25,{c:255 for c in CHANNELS},linear_light=True)
        a = render_gaussian_overlay(original,linear_light=True,normalize_designs=True)
        b = render_gaussian_overlay(original,linear_light=True,normalize_designs=True,compensate_area=True)
        np.testing.assert_array_equal(np.asarray(a.rgb),np.asarray(b.rgb))
        subset = [d for d in designs if d["x_shift"]==d["y_shift"]==0]
        aligned = AlignmentModel(subset,AlignmentSettings(),linear_light=True,normalize_designs=True,compensate_area=True)
        control = AlignmentModel(subset,AlignmentSettings(),exposure=4,linear_light=True,normalize_designs=True)
        np.testing.assert_array_equal(np.asarray(aligned.expected(subset).rgb),np.asarray(control.expected(subset).rgb))
        target = compensated.density*compensated.metadata["linear_rgb_gain"]
        optimized,metrics = optimize_designs(designs,target,linear_light=True,normalize_designs=True,compensate_area=True)
        self.assertEqual(optimized,designs)
        self.assertLess(metrics["optimized_rmse"],1e-12)

    def test_unequal_quadrants_balance_brightness_and_optimize_actual_positions(self):
        key = "quadrants_joint_6x3"
        scheme = ENCODING_SCHEMES[key]
        pixels = [[[128]*3 for _ in range(6*scheme.x_states)] for _ in range(8*scheme.y_states)]
        designs = compile_designs(pixels,25,{c:255 for c in CHANNELS},key,linear_light=True)
        result = render_gaussian_overlay(designs,linear_light=True,normalize_designs=True,compensate_area=True)
        for qx in (0,1):
            for qy in (0,1):
                x = round(result.rgb.width*(.25+.5*qx))
                y = round(result.rgb.height*(.25+.5*qy))
                np.testing.assert_allclose(np.asarray(result.rgb)[y,x],[128]*3,atol=1)
        baseline = compile_designs(pixels,100,{c:1 for c in CHANNELS},key,linear_light=True)
        target = np.full(result.density.shape,float(srgb_to_linear(128/255)))
        optimized,metrics = optimize_designs(baseline,target,linear_light=True,normalize_designs=True,compensate_area=True)
        rendered = render_gaussian_overlay(optimized,linear_light=True,normalize_designs=True,compensate_area=True)
        actual = np.mean((rendered.density*rendered.metadata["linear_rgb_gain"]-target)**2)
        self.assertAlmostEqual(actual,metrics["optimized_rmse"]**2)
        self.assertLess(metrics["optimized_rmse"],metrics["baseline_rmse"])

    def test_optimizers_share_linear_target_gain_and_display(self):
        key = 'joint_7x3'
        scheme = ENCODING_SCHEMES[key]
        pixels = [[[128]*3 for _ in range(6*scheme.x_states)] for _ in range(8*scheme.y_states)]
        designs = compile_designs(pixels,100,{c:1 for c in CHANNELS},key,linear_light=True)
        baseline = render_gaussian_overlay(designs,linear_light=True,normalize_designs=True)
        target = np.full(baseline.density.shape,float(srgb_to_linear(128/255)))
        optimized,metrics = optimize_designs(designs,target,linear_light=True,normalize_designs=True)
        rendered = render_gaussian_overlay(optimized,linear_light=True,normalize_designs=True)
        error = np.mean((rendered.density*rendered.metadata['linear_rgb_gain']-target)**2)
        self.assertAlmostEqual(metrics['optimized_rmse']**2,error)
        self.assertLess(metrics['optimized_rmse'],metrics['baseline_rmse'])
        model = AlignmentModel(designs,AlignmentSettings(),linear_light=True,normalize_designs=True)
        target = np.full(model.expected(designs).density.shape,float(srgb_to_linear(128/255)))
        optimized,metrics = model.optimize(designs,target)
        result = model.expected(optimized)
        np.testing.assert_array_equal(np.asarray(result.rgb),np.rint(linear_to_srgb(result.density*model.gain)*255).astype(np.uint8))
        self.assertLessEqual(metrics['training_optimized']['objective_mse'],metrics['training_baseline']['objective_mse'])
