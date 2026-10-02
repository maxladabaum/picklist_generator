import copy
import unittest
from types import SimpleNamespace
from unittest import mock
from picklist_app import OrigamiTemplateView
from template_generator import normalize_logical_bit_schema, render_barcode_template


class BrightnessFactorsTests(unittest.TestCase):
    def setUp(self):
        self.schema = {"format": "paint-analysis-logical-bits-v1",
                       "physical_rows": 1, "physical_columns": 3,
                       "logical_bits": [
                           {"id": "A", "physical_sites": [[1, 1]], "brightness_factor": 2},
                           {"id": "B", "physical_sites": [[1, 2]], "brightness_factor": 0.2},
                           {"id": "C", "physical_sites": [[1, 3]]}]}

    def test_factors_survive_normalization_and_template_metadata(self):
        schema = normalize_logical_bit_schema(self.schema, 1, 3)
        self.assertEqual([b["brightness_factor"] for b in schema["logical_bits"]], [2, 0.2, 1])
        *_, metadata = render_barcode_template(1, 3, [(0, 0), (0, 1), (0, 2)],
                                               logical_schema=schema, active_logical_bits=["A", "B", "C"])
        self.assertEqual([b["brightness_factor"] for b in metadata["logical_model"]["logical_bits"]], [2, 0.2, 1])
        # Brightness calibrates detection only, not rendered geometry/intensity.
        normal = copy.deepcopy(schema)
        for bit in normal["logical_bits"]:
            bit["brightness_factor"] = 1
        scaled_image = render_barcode_template(1, 3, [(0, 0), (0, 1), (0, 2)], logical_schema=schema)
        normal_image = render_barcode_template(1, 3, [(0, 0), (0, 1), (0, 2)], logical_schema=normal)
        self.assertEqual(scaled_image[:3], normal_image[:3])

    def test_reject_invalid_factors(self):
        for factor in (0, -1, float("inf"), float("nan"), True, "2", None):
            schema = copy.deepcopy(self.schema)
            schema["logical_bits"][0]["brightness_factor"] = factor
            with self.subTest(factor=factor), self.assertRaisesRegex(ValueError, "brightness_factor"):
                normalize_logical_bit_schema(schema, 1, 3)


class GroupEditorBrightnessTests(unittest.TestCase):
    def test_create_update_and_invalid_input_preserve_schema(self):
        def variable(value):
            return SimpleNamespace(get=lambda: value)
        app = SimpleNamespace(
            selected_group_ids=[], selected_group_id=None,
            group_name_var=variable("A"), group_role_var=variable("Digital bit"),
            group_active_var=variable(True), group_brightness_var=variable("0.2"),
            logical_schema=None, _built_shape=(1, 3),
            _selected_sites=lambda: {(0, 0)}, _group_by_id=lambda _: None,
            _active_bit_ids=lambda: [], _all_groups=lambda: [],
            _unique_group_id=lambda *_: "a", _build_logical_controls=mock.Mock(),
            _settings_changed=mock.Mock())
        OrigamiTemplateView._store_group(app, create=True)
        self.assertEqual(app.logical_schema["logical_bits"][0]["brightness_factor"], 0.2)
        app.group_brightness_var = variable("2")
        OrigamiTemplateView._store_group(app, create=False)
        self.assertEqual(app.logical_schema["logical_bits"][0]["brightness_factor"], 2.)
        before = copy.deepcopy(app.logical_schema)
        app.group_brightness_var = variable("0")
        with mock.patch("picklist_app.messagebox.showerror") as error:
            OrigamiTemplateView._store_group(app, create=False)
            error.assert_called_once()
        self.assertEqual(app.logical_schema, before)
