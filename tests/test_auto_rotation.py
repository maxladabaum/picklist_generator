import unittest
from unittest.mock import patch
from PIL import Image
from image_encoding import SELECTABLE_SCHEMES, best_image_rotation, bin_image
from exhaustive_pdf import report_jobs
import test_image_encoding


class AutoRotationWorkflowTests(unittest.TestCase):
    setUp = test_image_encoding.ImageWorkflowTests.setUp

    def test_upload_selects_best_fit_and_manual_rotation_survives_setting_changes(self):
        view = self.view
        source = Image.new('RGB', (60, 100), 'red')
        source.paste('blue', (0, 50, 60, 100))
        source.save(self.image_path)
        with patch('rgb_image_view.filedialog.askopenfilename', return_value=str(self.image_path)):
            view._upload()
        self.assertEqual(view.rotation.get(), '90')
        self.assertEqual(view.pixels, bin_image(self.image_path, view.fit_mode.get(), 90, view._scheme_id(), linear_light=True))
        self.assertEqual(report_jobs(self.image_path, [view._scheme_id()])[0][1], 90)
        view._rotate(-90)
        self.assertEqual(view.rotation.get(), '0')
        view.gaussian_exposure.set('0.8')
        view._light_changed()
        self.assertEqual(view.rotation.get(), '0')
        view._load(auto_rotate=True)
        self.assertEqual(view.rotation.get(), '90')

    def test_layout_changes_reselect_orientation_and_new_landscape_upload_uses_zero(self):
        view = self.view
        Image.new('RGB', (60, 100), 'red').save(self.image_path)
        with patch('rgb_image_view.filedialog.askopenfilename', return_value=str(self.image_path)):
            view._upload()
        self.assertEqual(view.rotation.get(), '90')
        key = 'tiling_0_4_0_0'
        view.scheme.set(SELECTABLE_SCHEMES[key].label)
        self.assertEqual(view.rotation.get(), '0')
        self.assertEqual(int(view.rotation.get()), best_image_rotation(self.image_path, key))
        self.assertTrue(all(d['origami_rotation_degrees_clockwise'] == 0 for d in view.designs))
        view.scheme.set(SELECTABLE_SCHEMES['fixed_4x4'].label)
        self.assertEqual(view.rotation.get(), '90')
        Image.new('RGB', (100, 60), 'blue').save(self.image_path)
        with patch('rgb_image_view.filedialog.askopenfilename', return_value=str(self.image_path)):
            view._upload()
        self.assertEqual(view.rotation.get(), '0')
