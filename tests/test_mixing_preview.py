"""Check target concentrations through the actual preview generation path."""

import tempfile
import tkinter as tk
import unittest
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from picklist_app import PicklistApp


class MixingPreviewTests(unittest.TestCase):
    def test_independent_plate_targets_reach_requested_final_concentrations(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "base.csv"
            base.write_text("Well Position,Sequence Name,Sequence\nA01,one,AAA\nA02,two,CCC\nA03,three,GGG\n")
            replacement = Path(folder) / "replacement.csv"
            replacement.write_text("Well,Name,Sequence,Replace Well\nB01,P1,TTT,A01\nB02,R1,CTT,A02\n")
            for fraction, transfer_volume, repeats, base_target in (
                (1, 50, 1, 50), (1, 50, 2, 50), (0.5, 500, 1, 50), (1, 50, 1, 100),
            ):
                with self.subTest(fraction=fraction, repeats=repeats, base_target=base_target):
                    fields = {
                        "base_path": str(base), "transfer_volume": str(transfer_volume),
                        "transfers_per_source": str(repeats), "destination_wells": "A01",
                        "source_plate_type": "384PP_AQ_BP", "destination_plate": "Destination",
                        "max_volume": "12.5", "staple": "200000,1,10,20",
                        "scaffold": "400,0.1,3,4", "magnesium": "0,0.2,100,1",
                        "sodium": "0,0.3,2,100", "te_10x": "0,10,1,2",
                        "desired": "100,{},10,2,12,5".format(base_target),
                    }
                    app = SimpleNamespace(**{
                        name: Mock(get=Mock(return_value=value)) for name, value in fields.items()
                    })
                    app.set_views = []
                    for name, title, stock, target in (
                        ("P1", "PAINT P1", 200000, 50),
                        ("R1", "LowConc PAINT R1", 20000, 5),
                    ):
                        app.set_views.append(SimpleNamespace(
                            site_concentrations_nm={"P1": 100} if name == "P1" else {},
                            concentration_nm=stock, desired_concentration_nm=Mock(get=Mock(return_value=str(target))),
                            selected=lambda name=name: [name], title=title, plate_name=title,
                            site_fractions={name: fraction},
                            path_var=Mock(get=Mock(return_value=str(replacement))),
                        ))
                    app.check_selections = lambda: True
                    app._selected_hinge_type = lambda: "original"
                    app._replacement_selection_panels = lambda: []
                    for name in ("_update_result_action_buttons", "_fill_tree", "pick_tree",
                                 "mix_tree", "summary", "status"):
                        setattr(app, name, Mock())
                    app.summary.get.return_value = ""
                    with patch("picklist_app.messagebox.showerror") as error:
                        PicklistApp.generate(app, Mock(), Mock())
                        error.assert_not_called()
                    pool_ul = sum(row["Transfer Volume"] for row in app.last_picklist) / 1000
                    volumes = [row["Volume_uL"] for row in app.last_mix]
                    aliquot_fraction = volumes[0] / pool_ul
                    for row in app.last_picklist:
                        plate = row["Source Plate Name"]
                        if plate == "PAINT P1":
                            stock, expected = 200000, 100 * fraction
                        elif plate == "LowConc PAINT R1":
                            stock, expected = 20000, 5 * fraction
                        else:
                            stock = 200000
                            expected = {"A01": 100 * (1 - fraction),
                                        "A02": 5 * (1 - fraction), "A03": base_target}[row["Source Well"]]
                        actual = stock * row["Transfer Volume"] * repeats / 1000 * aliquot_fraction / 100
                        self.assertAlmostEqual(actual, expected)
                    stocks = [[float(value) for value in fields[key].split(',')]
                              for key in ('staple', 'scaffold', 'te_10x', 'magnesium', 'sodium')]
                    for component, target in enumerate((2, 12, 5), start=1):
                        actual = sum(volume * stock[component] for volume, stock in zip(volumes, stocks)) / 100
                        self.assertAlmostEqual(actual, target)
                    self.assertAlmostEqual(sum(volumes), 100)
                    for invalid_target in ("", "0", "nan", "not a number"):
                        app.set_views[0].desired_concentration_nm.get.return_value = invalid_target
                        app._fill_tree.reset_mock()
                        with patch("picklist_app.messagebox.showerror") as error:
                            PicklistApp.generate(app, Mock(), Mock())
                            error.assert_called_once()
                            self.assertIn("PAINT P1", error.call_args.args[1])
                        app._fill_tree.assert_not_called()

    def test_settings_edits_invalidate_preview_and_disable_export(self):
        interpreter = tk.Tcl()
        names = (
            "base_path", "destination_wells", "destination_plate", "source_plate_type",
            "transfers_per_source", "transfer_volume", "max_volume", "hinge_type",
            "staple", "scaffold", "magnesium", "sodium", "te_10x", "desired",
        )
        app = SimpleNamespace(**{name: tk.StringVar(interpreter, "old") for name in names})
        target = tk.StringVar(interpreter, "")
        app.set_views = [SimpleNamespace(desired_concentration_nm=target)]
        app._advancing_destination = False
        for name in ("_fill_tree", "pick_tree", "mix_tree", "summary", "status",
                     "save_run_button", "send_storage_button"):
            setattr(app, name, Mock())
        app._invalidate_preview = lambda *args: PicklistApp._invalidate_preview(app, *args)
        app._update_result_action_buttons = lambda: PicklistApp._update_result_action_buttons(app)
        PicklistApp._bind_preview_updates(app)
        for variable in [*(getattr(app, name) for name in names), target]:
            with self.subTest(variable=str(variable)):
                app.last_picklist, app.last_mix = [{"old": True}], [{"old": True}]
                app.last_run_saved = app.last_run_stored = False
                variable.set("changed")
                self.assertEqual(app.last_picklist, [])
                self.assertEqual(app.last_mix, [])
                app.save_run_button.configure.assert_called_with(state="disabled")
                app.send_storage_button.configure.assert_called_with(state="disabled")
        # Automatic destination advancement after saving/storage retains the run.
        app.last_picklist, app.last_mix = [{"old": True}], [{"old": True}]
        app._advancing_destination = True
        app.destination_wells.set("A02")
        self.assertTrue(app.last_picklist)
        self.assertTrue(app.last_mix)
        app._advancing_destination = False
        app.destination_wells.set("A03")
        self.assertEqual(app.last_picklist, [])

    def test_plate_targets_are_included_in_staple_buffer_and_water_volumes(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "base.csv"
            base.write_text("Well Position,Sequence Name,Sequence\nA01,one,AAA\nA02,two,CCC\n")
            replacement = Path(folder) / "replacement.csv"
            replacement.write_text("Well,Name,Sequence,Replace Well\nB01,R1,TTT,A01\n")
            for target, concentration in product((10, 20, 50), (200000, 20000)):
                for repeats in (1, 2):
                    with self.subTest(target=target, repeats=repeats, concentration=concentration):
                        fields = {
                            "base_path": str(base), "transfer_volume": "25",
                            "transfers_per_source": str(repeats), "destination_wells": "A01",
                            "source_plate_type": "384PP_AQ_BP", "destination_plate": "Destination",
                            "max_volume": "12.5", "staple": "200000,1,0,0",
                            "scaffold": "400,0.1,0,0", "magnesium": "0,0,100,0",
                            "sodium": "0,0,0,100", "te_10x": "0,10,0,0",
                            "desired": "500,10,1,1,12,5",
                        }
                        app = SimpleNamespace(**{
                            name: Mock(get=Mock(return_value=value)) for name, value in fields.items()
                        })
                        app.set_views = [SimpleNamespace(
                            desired_concentration_nm=Mock(get=Mock(return_value=str(target))),
                            concentration_nm=concentration,
                            site_concentrations_nm={},
                            site_fractions={"R1": 0.4} if target == 50 else {},
                            selected=lambda: ["R1"], title="Replacement", plate_name="Replacement",
                            path_var=Mock(get=Mock(return_value=str(replacement))),
                        )]
                        app.check_selections = lambda: True
                        app._selected_hinge_type = lambda: "original"
                        app._replacement_selection_panels = lambda: []
                        for name in ("_update_result_action_buttons", "_fill_tree", "pick_tree",
                                     "mix_tree", "summary", "status"):
                            setattr(app, name, Mock())
                        app.summary.get.return_value = ""
                        with patch("picklist_app.messagebox.showerror") as error:
                            PicklistApp.generate(app, Mock(), Mock())
                            error.assert_not_called()
                        fraction = 0.4 if target == 50 else 1.0
                        ratio = 200000 / concentration
                        staple_ul = 0.025 * (1 + (target / 10) * (1 - fraction + fraction * ratio))
                        if target == 50:
                            self.assertEqual(
                                [r["Transfer Volume"] for r in app.last_picklist if r["Source Well"] == "B01"],
                                [50 * ratio] * repeats,
                            )
                        self.assertAlmostEqual(
                            sum(row["Transfer Volume"] for row in app.last_picklist) / 1000,
                            staple_ul * repeats,
                        )
                        volumes = [row["Volume_uL"] for row in app.last_mix]
                        self.assertAlmostEqual(volumes[0], staple_ul)
                        self.assertAlmostEqual(volumes[2], (500 - 1.25 * 0.1 - staple_ul) / 10)
                        self.assertAlmostEqual(volumes[5], 500 - sum(volumes[:5]))
                        self.assertAlmostEqual(sum(volumes), 500)


if __name__ == "__main__":
    unittest.main()
