"""Regression checks for replacement panel reuse and selection updates."""

import tkinter as tk
import unittest
from unittest.mock import patch

from picklist_app import SHEET_DIR, SetView, panel_definitions, SHIFT_STATES, COLOR_BITS


class MonaLisaViewTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        title, filename, plate, panels = panel_definitions()[0]
        self.view = SetView(self.root, title, SHEET_DIR / filename, plate, panels)


    def test_tabs_share_editable_base_volume(self):
        view = self.view
        title, filename, plate, panels = panel_definitions()[0]
        other = SetView(self.root, title, SHEET_DIR / filename, plate, panels,
                        base_transfer_volume=view.base_transfer_volume)
        view.base_transfer_volume.set("100")
        self.assertEqual(other.base_transfer_volume.get(), "100")
        self.assertEqual(tuple(view.percentage_picker["values"]), tuple(other.percentage_picker["values"]))
        other.base_transfer_volume.set("200")
        self.assertEqual(view.base_transfer_volume.get(), "200")
        self.assertEqual(len(view.percentage_picker["values"]), 9)


    def test_r1_color_tabs_share_extensions_and_scale_highlight_opacity(self):
        definitions = {item[0]: item for item in panel_definitions()}
        original = definitions["LowConc PAINT R1"]
        for title, color in (("R1 R", "#ff0000"), ("R1 B", "#0000ff"), ("R1 G", "#00ff00")):
            with self.subTest(title=title):
                _, filename, plate, panels = definitions[title]
                self.assertEqual((filename, plate, panels), original[1:])
                view = SetView(self.root, title, SHEET_DIR / filename, plate, panels)
                self.assertEqual(view.excess_multiplier.get(), "5")
                self.assertEqual(view.desired_concentration_nm.get(), "25")
                self.assertEqual(view.concentration_nm, 20000.0)
                self.assertEqual(view.plate_name, "SourcePlate7[7]")
                view.base_transfer_volume.set("100")  # Makes the tested 50% split droplet-compatible.
                name = next(name for name in view.circle_sites if view.selectable[name])
                view.variables[name].set(True)
                view._on_square_toggled(name)
                canvas, ring, _, _ = view.circle_sites[name]
                self.assertEqual(canvas.itemcget(ring, "fill"), color)
                view.variables[name].set(False)
                view._on_square_toggled(name)
                view.replacement_percentage.set("50%")
                view.variables[name].set(True)
                view._on_square_toggled(name)
                expected = {"R1 R": "#8a0c0e", "R1 B": "#0a0c8e", "R1 G": "#0a8c0e"}[title]
                self.assertEqual(canvas.itemcget(ring, "fill"), expected)
                view.clear()
                self.assertEqual(canvas.itemcget(ring, "fill"), "")


    def test_shift_dropdowns_control_all_six_bits_without_changing_image(self):
        definitions = {item[0]: item for item in panel_definitions()}
        for title, color_bits in COLOR_BITS.items():
            _, filename, plate, panels = definitions[title]
            view = SetView(self.root, title, SHEET_DIR / filename, plate, panels)
            self.assertEqual(view.selected(), [])
            image_site = "U-Apt_H07-03_R1"
            view.variables[image_site].set(True)
            view._on_square_toggled(image_site)
            self.assertTrue(view.encoding_enabled.get())
            for x_label, x_bits in SHIFT_STATES.items():
                for y_label, y_bits in SHIFT_STATES.items():
                    with self.subTest(title=title, x=x_label, y=y_label):
                        view.x_shift.set(x_label)
                        view.y_shift.set(y_label)
                        view._shift_changed()
                        states = color_bits + x_bits + y_bits
                        expected = {name for name, bit in view.encoded_sites.items() if states[bit]}
                        self.assertEqual(set(view.selected()), expected | {image_site})
            locked = "U-Apt_H01-11_R1"
            view.variables[locked].set(not color_bits[0])
            view._on_square_toggled(locked)
            self.assertEqual(view.variables[locked].get(), color_bits[0])
            view.refresh()
            self.assertEqual(set(view.selected()), {name for name, bit in view.encoded_sites.items()
                                                   if (color_bits + (True,) * 4)[bit]})
            view.encoding_enabled.set(False)
            view._encoding_changed()
            self.assertEqual(view.selected(), [])
            view.clear()
            self.assertEqual(view.x_shift.get(), next(iter(SHIFT_STATES)))
            self.assertEqual(view.y_shift.get(), next(iter(SHIFT_STATES)))
            self.assertEqual({name for name, variable in view.variables.items() if variable.get()},
                             {name for name, bit in view.encoded_sites.items() if bit < 2 and color_bits[bit]})


    def test_color_bits_stay_highlighted_when_disabled_or_cleared(self):
        for title, filename, plate, panels in panel_definitions():
            if title not in COLOR_BITS:
                continue
            with self.subTest(title=title):
                view = SetView(self.root, title, SHEET_DIR / filename, plate, panels)
                for action in (lambda: None, view.clear, view._encoding_changed, view.refresh):
                    action()
                    self.assertEqual(view.selected(), [])
                    for name, bit in view.encoded_sites.items():
                        if bit >= 2:
                            continue
                        expected = COLOR_BITS[title][bit]
                        self.assertEqual(view.variables[name].get(), expected)
                        canvas, ring, _, _ = view.circle_sites[name]
                        self.assertEqual(canvas.itemcget(ring, "fill"), view.selection_color if expected else "")


