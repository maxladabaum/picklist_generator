"""Check excess volumes through the actual preview generation path."""

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from picklist_app import PicklistApp


class MixingPreviewTests(unittest.TestCase):
    def test_settings_edits_invalidate_preview_and_disable_export(self):
        interpreter = tk.Tcl()
        names = (
            "base_path", "destination_wells", "destination_plate", "source_plate_type",
            "transfers_per_source", "transfer_volume", "max_volume", "hinge_type",
            "staple", "scaffold", "magnesium", "sodium", "te_10x", "desired",
        )
        app = SimpleNamespace(**{name: tk.StringVar(interpreter, "old") for name in names})
        excess = tk.StringVar(interpreter, "1")
        app.set_views = [SimpleNamespace(excess_multiplier=excess)]
        app._advancing_destination = False
        for name in ("_fill_tree", "pick_tree", "mix_tree", "summary", "status",
                     "save_run_button", "send_storage_button"):
            setattr(app, name, Mock())
        app._invalidate_preview = lambda *args: PicklistApp._invalidate_preview(app, *args)
        app._update_result_action_buttons = lambda: PicklistApp._update_result_action_buttons(app)
        PicklistApp._bind_preview_updates(app)
        for variable in [*(getattr(app, name) for name in names), excess]:
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

    def test_excess_is_included_in_staple_buffer_and_water_volumes(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "base.csv"
            base.write_text("Well Position,Sequence Name,Sequence\nA01,one,AAA\nA02,two,CCC\n")
            replacement = Path(folder) / "replacement.csv"
            replacement.write_text("Well,Name,Sequence,Replace Well\nB01,R1,TTT,A01\n")
            for multiplier in (1, 1.5, 5):
                for repeats in (1, 2):
                    with self.subTest(multiplier=multiplier, repeats=repeats):
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
                            site_fractions={"R1": 0.4} if multiplier == 5 else {},
                            selected=lambda: ["R1"], title="Replacement", plate_name="Replacement",
                            path_var=Mock(get=Mock(return_value=str(replacement))),
                            excess_multiplier=Mock(get=Mock(return_value=str(multiplier))),
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
                        staple_ul = 0.025 * (1 + multiplier)
                        if multiplier == 5:
                            self.assertEqual(
                                [r["Transfer Volume"] for r in app.last_picklist if r["Source Well"] == "B01"],
                                [50] * repeats,
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
