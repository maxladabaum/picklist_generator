import math
import unittest

import numpy as np

from gaussian_reconstruction import render_gaussian_overlay
from image_encoding import compile_designs, CHANNELS


def emitter(x=28.8375, y=18.9375, fraction=1, channel=0, angle=0):
    return {"channel": channel, "encoding_scheme": "fixed_4x4", "origami_rotation_degrees_clockwise": angle,
            "image_sites": [{"x_nm": x, "y_nm": y, "replacement_fraction": fraction}],
            "site_fractions": {"barcode": 1}}


class GaussianReconstructionTests(unittest.TestCase):
    def test_isolated_emitter_has_requested_physical_fwhm_and_unit_integral(self):
        result = render_gaussian_overlay([emitter()])
        self.assertEqual(result.metadata["fwhm_nm"], 7.5)
        self.assertAlmostEqual(result.metadata["sigma_nm"], 7.5 / math.sqrt(8*math.log(2)))
        plane = result.density[:,:,0]
        y, x = np.unravel_index(np.argmax(plane), plane.shape)
        dx, dy = result.metadata["raster_pixel_x_nm"], result.metadata["raster_pixel_y_nm"]
        self.assertAlmostEqual(result.metadata["first_sample_x_nm"] + x*dx,28.8375)
        self.assertAlmostEqual(result.metadata["first_sample_y_nm"] + y*dy,18.9375)
        self.assertAlmostEqual(plane[y,x+25] / plane[y,x], .5, places=10)
        self.assertAlmostEqual(plane[y+25,x] / plane[y,x], .5, places=10)
        self.assertAlmostEqual(float(plane.sum())*dx*dy,1,places=7)
        self.assertEqual(np.count_nonzero(result.density[:,:,1:]),0)

    def test_signals_sum_and_fraction_controls_integrated_intensity(self):
        full = render_gaussian_overlay([emitter()])
        half = render_gaussian_overlay([emitter(fraction=.5)])
        double = render_gaussian_overlay([emitter(),emitter()])
        np.testing.assert_allclose(half.density,full.density*.5)
        np.testing.assert_allclose(double.density,full.density*2)
        wide = render_gaussian_overlay([emitter()],fwhm_nm=10)
        self.assertLess(wide.density.max(),full.density.max())
        self.assertAlmostEqual(float(wide.density.sum()) * .15**2,1,places=4)

    def test_pseudocolors_mix_and_exposure_never_changes_raw_signal(self):
        yellow = render_gaussian_overlay([emitter(),emitter(channel=1)],exposure=1)
        exposed = render_gaussian_overlay([emitter(),emitter(channel=1)],exposure=100)
        np.testing.assert_allclose(yellow.density[:,:,0],yellow.density[:,:,1])
        self.assertEqual(np.count_nonzero(yellow.density[:,:,2]),0)
        np.testing.assert_array_equal(yellow.density,exposed.density)
        self.assertEqual(np.asarray(exposed.rgb)[:,:,2].max(),0)
        self.assertEqual(np.asarray(exposed.rgb)[:,:,0].max(),255)
        self.assertGreater(exposed.metadata["clipped_pixel_fraction"],yellow.metadata["clipped_pixel_fraction"])

    def test_shifts_and_origami_rotation_move_gaussians_in_nm(self):
        base = render_gaussian_overlay([emitter()])
        shifted = render_gaussian_overlay([emitter(x=28.8375+2.475,y=18.9375+1.275)])
        y,x = np.unravel_index(np.argmax(shifted.density[:,:,0]),shifted.density.shape[:2])
        self.assertLessEqual(abs(shifted.metadata["first_sample_x_nm"] + x*.15 - (28.8375+2.475)),.075+1e-9)
        self.assertLessEqual(abs(shifted.metadata["first_sample_y_nm"] + y*.15 - (18.9375+1.275)),.075+1e-9)
        for angle in (90,180,270):
            result = render_gaussian_overlay([emitter(angle=angle)])
            np.testing.assert_allclose(result.density,np.rot90(base.density,-angle//90))
            self.assertEqual(result.metadata["origami_rotation_degrees_clockwise"],angle)

    def test_barcode_is_excluded_and_no_channel_or_scheme_is_autoscaled(self):
        dark = render_gaussian_overlay([emitter(fraction=0)])
        self.assertEqual(np.count_nonzero(dark.density),0)
        fields = []
        for scheme_id, width, height in (("fixed_4x4",24,32),("joint_7x3",42,24)):
            pixels = [[[255,0,0] for _ in range(width)] for _ in range(height)]
            designs = compile_designs(pixels,50,{color:5 for color in CHANNELS},scheme_id)
            fields.append(render_gaussian_overlay(designs))
        centers = [item.density[item.density.shape[0]//2,item.density.shape[1]//2,0] for item in fields]
        self.assertAlmostEqual(centers[1]/centers[0],63/48,places=4)
        self.assertEqual(fields[0].metadata["reference_area_nm2"],fields[1].metadata["reference_area_nm2"])

    def test_invalid_width_exposure_and_mixed_frames_are_rejected(self):
        for value in (0,-1,float("nan"),float("inf")):
            with self.assertRaises(ValueError):
                render_gaussian_overlay([emitter()],fwhm_nm=value)
            with self.assertRaises(ValueError):
                render_gaussian_overlay([emitter()],exposure=value)
        with self.assertRaises(ValueError):
            render_gaussian_overlay([emitter()],fwhm_nm=.01)
        with self.assertRaises(ValueError):
            render_gaussian_overlay([emitter(),emitter(angle=90)])
        with self.assertRaises(ValueError):
            render_gaussian_overlay([])


if __name__ == "__main__":
    unittest.main()
