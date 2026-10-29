from copy import deepcopy
from dataclasses import replace
import unittest
import numpy as np
from alignment_reconstruction import (AlignmentModel, AlignmentSettings, transformed_sites,
                                      DEFAULT_CENTER_X, DEFAULT_CENTER_Y, VALIDATION_SEED)
from image_encoding import CHANNELS, compile_designs


def designs():
    full = compile_designs([[[128,100,64] for _ in range(24)] for _ in range(32)],100,{c:1 for c in CHANNELS})
    # A small matched set keeps tests of statistical identities cheap.
    selected = [d for d in full if d['x_shift']==1 and d['y_shift']==1]
    for design in selected:
        design['image_sites'] = design['image_sites'][:2]
    return selected


class AlignmentTests(unittest.TestCase):
    def test_rigid_error_precedes_shift_and_preserves_pair_distances(self):
        design = designs()[0]
        settings = AlignmentSettings(center_x_nm=0,center_y_nm=0)
        points = transformed_sites(design,settings,[2,3,90])
        shift = np.array([design['x_shift_nm'],design['y_shift_nm']])
        original = np.array([[s['x_nm'],s['y_nm']] for s in design['image_sites']])
        expected = (original-shift)@np.array([[0,1],[-1,0]])+shift+[2,3]
        np.testing.assert_allclose(points,expected,atol=1e-12)
        self.assertAlmostEqual(np.linalg.norm(points[1]-points[0]),np.linalg.norm(original[1]-original[0]))
        np.testing.assert_allclose(transformed_sites(design,settings,[2,3,0])-original,[[2,3],[2,3]])
        self.assertAlmostEqual(DEFAULT_CENTER_X,-9.95)
        self.assertAlmostEqual(DEFAULT_CENTER_Y,17.85)

    def test_zero_error_has_no_count_dependence_and_no_extra_blur(self):
        ds = designs()
        settings = AlignmentSettings(0,0,0,3)
        model = AlignmentModel(ds,settings)
        expected = model.expected(ds)
        example = model.example(ds,seed=12)
        np.testing.assert_allclose(expected.density,example.density,atol=1e-15)
        point_kernel = model.kernel(transformed_sites(ds[0],settings,[0,0,0]))
        np.testing.assert_allclose(expected.density.reshape(-1,3),point_kernel@model.weights(ds),atol=1e-15)
        values = model.evaluate(ds,np.zeros_like(expected.density))
        self.assertLess(values['alignment_variance'],1e-16)

    def test_covariance_models_correlated_sites_and_count_reduces_variance(self):
        ds = designs()
        settings = AlignmentSettings(2,1,2,1)
        model = AlignmentModel(ds,settings)
        target = np.zeros_like(model.expected(ds).density)
        one = model.evaluate(ds,target)
        ten = AlignmentModel(ds,replace(settings,origami_per_design=10))
        many = ten.evaluate(ds,target)
        np.testing.assert_array_equal(model.expected(ds).density,ten.expected(ds).density)
        self.assertAlmostEqual(one['alignment_variance'],10*many['alignment_variance'])
        self.assertGreater(one['alignment_variance'],0)
        self.assertGreater(abs(model.covariances[0][0,1]),1e-8)
        penalized = AlignmentModel(ds,replace(settings,variability_penalty=2)).evaluate(ds,target)
        self.assertAlmostEqual(penalized['objective_mse'],one['mean_image_mse']+3*one['alignment_variance'])

    def test_example_reproducibility_independent_colors_and_rotation(self):
        ds = designs()
        model = AlignmentModel(ds,AlignmentSettings(origami_per_design=2))
        first = model.example(ds,seed=50)
        np.testing.assert_array_equal(first.density,model.example(ds,seed=50).density)
        self.assertFalse(np.array_equal(first.density,model.example(ds,seed=51).density))
        rotated = deepcopy(ds)
        for d in rotated:
            d['origami_rotation_degrees_clockwise'] = 90
        other = AlignmentModel(rotated,model.settings)
        np.testing.assert_allclose(other.expected(rotated).density,np.rot90(model.expected(ds).density,-1))
        np.testing.assert_allclose(other.example(rotated,seed=50).density,np.rot90(first.density,-1))

    def test_robust_optimization_keeps_barcodes_and_droplets_and_has_independent_validation(self):
        ds = designs()
        untouched = deepcopy(ds)
        model = AlignmentModel(ds,AlignmentSettings(2,2,3,2,variability_penalty=1))
        target = np.zeros_like(model.expected(ds).density)
        target[:,:,0] = .1
        optimized,metrics = model.optimize(ds,target)
        self.assertLessEqual(metrics['training_optimized']['objective_mse'],metrics['training_baseline']['objective_mse'])
        self.assertEqual(ds,untouched)
        for before,after in zip(ds,optimized):
            image_names = {s['name'] for s in after['image_sites']}
            for name,value in before['site_fractions'].items():
                if name not in image_names:
                    self.assertEqual(value,after['site_fractions'][name])
            for site in after['image_sites']:
                self.assertEqual(site['replacement_nl']+site['original_nl'],100)
                self.assertEqual(site['replacement_nl']%25,0)
                self.assertEqual(site['original_nl']%25,0)
        validation = AlignmentModel(ds,model.settings,seed=VALIDATION_SEED)
        self.assertFalse(np.array_equal(model.mean,validation.mean))
        self.assertTrue(np.isfinite(validation.evaluate(optimized,target)['expected_mse']))

    def test_invalid_alignment_inputs_rejected(self):
        for kwargs in ({'translation_x_nm':-1},{'rotation_deg':float('nan')},
                       {'center_y_nm':float('inf')},{'origami_per_design':0},
                       {'origami_per_design':1.5},{'variability_penalty':-1}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                AlignmentSettings(**kwargs).validate()
