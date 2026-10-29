import unittest
from copy import deepcopy
import numpy as np
from image_encoding import compile_designs, geometry, CHANNELS, ENCODING_SCHEMES
from gaussian_reconstruction import render_gaussian_overlay, REFERENCE_AREA_NM2
from gaussian_optimization import optimize_designs


class GaussianOptimizationTests(unittest.TestCase):
    def test_all_schemes_improve_and_preserve_barcodes_and_droplets(self):
        for index, scheme in enumerate(ENCODING_SCHEMES):
            with self.subTest(scheme=scheme):
                info = geometry(scheme)
                pixels = [[[150 if x < info['columns']//2 else 20, 60, 0]
                           for x in range(info['columns'])] for y in range(info['rows'])]
                designs = compile_designs(pixels, 100, dict(zip(CHANNELS,(1,2,1.5))), scheme, (index%4)*90)
                untouched = deepcopy(designs)
                baseline = render_gaussian_overlay(designs)
                target = np.zeros_like(baseline.density)
                target[:,:target.shape[1]//2,0] = .6
                target[:,:,1] = .24
                result, metrics = optimize_designs(designs,target)
                actual = render_gaussian_overlay(result)
                before = np.mean((baseline.density*REFERENCE_AREA_NM2-target)**2)
                after = np.mean((actual.density*REFERENCE_AREA_NM2-target)**2)
                self.assertLess(after,before)
                self.assertAlmostEqual(metrics['optimized_rmse'],np.sqrt(after))
                self.assertEqual(designs,untouched)
                for old,new in zip(designs,result):
                    image_names = {site['name'] for site in old['image_sites']}
                    self.assertEqual(old['bits'],new['bits'])
                    for name,fraction in old['site_fractions'].items():
                        if name not in image_names:
                            self.assertEqual(new['site_fractions'][name],fraction)
                    for a,b in zip(old['image_sites'],new['image_sites']):
                        total = a['replacement_nl']+a['original_nl']
                        self.assertEqual(b['replacement_nl']+b['original_nl'],total)
                        self.assertEqual(b['replacement_nl']%25,0)
                        self.assertEqual(b['original_nl']%25,0)
                        self.assertGreaterEqual(b['original_nl'],0)
                        self.assertGreaterEqual(b['replacement_nl'],0)
                        self.assertEqual(new['site_fractions'][b['name']],b['replacement_nl']/total)

    def test_exact_achievable_target_and_black_do_not_regress(self):
        for value in (0,128):
            designs = compile_designs([[[value]*3 for _ in range(24)] for _ in range(32)],100,{c:1 for c in CHANNELS})
            target = render_gaussian_overlay(designs,4,.5).density*REFERENCE_AREA_NM2*.5
            result,metrics = optimize_designs(designs,target,4,.5)
            self.assertLessEqual(metrics['optimized_rmse'],1e-14)
            self.assertEqual(result,designs)

    def test_invalid_target_rejected(self):
        designs = compile_designs([[[0]*3 for _ in range(24)] for _ in range(32)],25,{c:1 for c in CHANNELS})
        with self.assertRaises(ValueError):
            optimize_designs(designs,np.zeros((1,1,3)))
