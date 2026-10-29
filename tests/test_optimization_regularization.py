import unittest
import numpy as np
from alignment_reconstruction import AlignmentModel, AlignmentSettings
from gaussian_optimization import optimize_designs
from gaussian_reconstruction import render_gaussian_overlay
from image_encoding import CHANNELS, compile_designs
from optimization_regularization import regularization_weight
from test_alignment_reconstruction import designs as small_designs


def fractions(designs):
    return np.array([s['replacement_fraction'] for d in designs for s in d['image_sites']])


class RegularizationTests(unittest.TestCase):
    def test_gaussian_shrinkage_preserves_edges_barcodes_and_droplets(self):
        pixels = [[[150 if x < 12 else 20, 60, 0] for x in range(24)] for _ in range(32)]
        designs = compile_designs(pixels, 100, {c:2 for c in CHANNELS})
        target = np.zeros_like(render_gaussian_overlay(designs).density)
        target[:,:target.shape[1]//2,0] = .6
        target[:,:,1] = .24
        raw, raw_metrics = optimize_designs(designs, target)
        regularized, metrics = optimize_designs(designs, target, regularization_strength=.1)
        difference = np.mean((fractions(regularized)-fractions(designs))**2)
        self.assertLess(difference, np.mean((fractions(raw)-fractions(designs))**2))
        self.assertAlmostEqual(metrics['regularization_penalty'], .1*difference)
        self.assertAlmostEqual(metrics['optimized_regularized_objective'], metrics['optimized_rmse']**2+.1*difference)
        self.assertLessEqual(metrics['optimized_regularized_objective'], metrics['baseline_regularized_objective'])
        zero, _ = optimize_designs(designs, target, regularization_strength=0)
        self.assertEqual(zero, raw)
        strong, _ = optimize_designs(designs, target, regularization_strength=1000)
        self.assertEqual(strong, designs)
        for a,b in zip(designs,regularized):
            self.assertEqual(a['bits'],b['bits'])
            for old,new in zip(a['image_sites'],b['image_sites']):
                self.assertEqual(new['replacement_nl']%25, 0)
                self.assertEqual(new['replacement_nl']+new['original_nl'],old['replacement_nl']+old['original_nl'])

    def test_alignment_shrinkage_and_reported_objective_match_actual_fractions(self):
        designs = small_designs()
        model = AlignmentModel(designs, AlignmentSettings(2, 1, 2, 5))
        target = np.zeros_like(model.expected(designs).density)
        raw, _ = model.optimize(designs, target)
        result, metrics = model.optimize(designs, target, regularization_strength=.1)
        difference = np.mean((fractions(result)-fractions(designs))**2)
        self.assertLess(difference, np.mean((fractions(raw)-fractions(designs))**2))
        self.assertAlmostEqual(metrics['regularization_penalty'], .1*difference)
        self.assertAlmostEqual(metrics['optimized_regularized_objective'], model.evaluate(result,target)['objective_mse']+.1*difference)
        self.assertLessEqual(metrics['optimized_regularized_objective'], metrics['baseline_regularized_objective'])
        strong, _ = model.optimize(designs, target, regularization_strength=1000)
        self.assertEqual(strong, designs)
        zero, _ = model.optimize(designs, target, regularization_strength=0)
        self.assertEqual(zero, raw)

    def test_resolution_normalization_and_validation(self):
        self.assertEqual(regularization_weight(.1, 100, 10), 1)
        self.assertEqual(regularization_weight(.1, 400, 10)/4, 1)
        self.assertEqual(regularization_weight(.1, 100, 20)*2, 1)
        for invalid in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                regularization_weight(invalid, 100, 10)


import test_image_encoding


class RegularizationWorkflowTests(unittest.TestCase):
    setUp = test_image_encoding.ImageWorkflowTests.setUp

    def test_setting_persists_and_reaches_both_optimizers(self):
        view = self.view
        view.regularization.set('0.07')
        self.assertEqual(self.app.config['image_optimization_regularization'], .07)
        view.image_path.set(str(self.image_path))
        view._load()
        view.optimize_enabled.set(True)
        for alignment in (False, True):
            view.alignment_enabled.set(alignment)
            view._volumes_changed()
            self.assertEqual(view.optimization_metrics['regularization_strength'], .07)
            self.assertEqual(view.runs, [])
        view.regularization.set('0')
        self.assertEqual(view.optimization_metrics['regularization_strength'], 0)
        view.regularization.set('-1')
        self.assertIsNone(view.reconstruction)
        self.assertIn('Regularization strength', view.status.get())
