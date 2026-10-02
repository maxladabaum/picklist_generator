"""Regression checks for replacement panel reuse and selection updates."""

import tkinter as tk
import unittest
from unittest.mock import patch

from picklist_app import SHEET_DIR, PicklistApp, SetView, panel_definitions
from types import SimpleNamespace


class ReplacementViewTests(unittest.TestCase):
    def test_site_targets_and_labels_follow_overrides_or_plate_default(self):
        view = self.view
        names = [name for name in view.circle_sites if view.selectable[name]][:2]
        view.new_selection_concentration.set("100")
        view.variables[names[0]].set(True)
        view._on_square_toggled(names[0])
        view.new_selection_concentration.set("")
        view.variables[names[1]].set(True)
        view._on_square_toggled(names[1])
        view.desired_concentration_nm.set("25")
        self.assertEqual(view.site_target(names[0]), 100)
        self.assertEqual(view.site_target(names[1]), 25)
        for name, expected in zip(names, ("100 nM", "25 nM")):
            canvas, ring, _, _ = view.circle_sites[name]
            self.assertEqual(canvas.itemcget("target_{}".format(ring), "text"), expected)
        panels = PicklistApp._replacement_selection_panels(SimpleNamespace(set_views=[view]))
        self.assertEqual(panels[0]["site_concentrations_nm"], dict(zip(names, (100, 25))))
        view.variables[names[0]].set(False)
        view._on_square_toggled(names[0])
        self.assertNotIn(names[0], view.site_concentrations_nm)
        view.variables[names[0]].set(True)
        view._on_square_toggled(names[0])
        self.assertEqual(view.site_target(names[0]), 25)
        view.clear()
        self.assertFalse(view.site_concentrations_nm)
        canvas, ring, _, _ = view.circle_sites[names[0]]
        self.assertEqual(canvas.itemcget("target_{}".format(ring), "text"), "")

    def test_invalid_selection_target_does_not_select_site(self):
        view = self.view
        name = next(name for name in view.circle_sites if view.selectable[name])
        for value in ("0", "-5", "nan", "oops"):
            view.new_selection_concentration.set(value)
            view.variables[name].set(True)
            view._on_square_toggled(name)
            self.assertFalse(view.variables[name].get())
            self.assertNotIn(name, view.site_concentrations_nm)

    def test_button_sites_show_target_text(self):
        title, filename, plate, panels = next(item for item in panel_definitions() if item[0] == "Aptamers")
        view = SetView(self.root, title, SHEET_DIR / filename, plate, panels)
        name = next(name for name in view.buttons if view.selectable[name])
        view.new_selection_concentration.set("100")
        view.variables[name].set(True)
        view._on_square_toggled(name)
        self.assertEqual(view.buttons[name].cget("text"), "100 nM")
        view.mark_conflicts({name})
        self.assertIn("100 nM", view.buttons[name].cget("text"))

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        title, filename, plate, panels = panel_definitions()[0]
        self.view = SetView(self.root, title, SHEET_DIR / filename, plate, panels)

    def test_low_concentration_r1_reuses_layout_with_distinct_stock(self):
        definitions = {title: (filename, plate, panels)
                       for title, filename, plate, panels in panel_definitions()}
        standard_file, standard_plate, standard_panels = definitions["PAINT R1"]
        filename, plate, panels = definitions["LowConc PAINT R1"]
        self.assertEqual(filename, standard_file)
        self.assertEqual(panels, standard_panels)
        self.assertNotEqual(plate, standard_plate)
        view = SetView(self.root, "LowConc PAINT R1", SHEET_DIR / filename, plate, panels)
        self.assertEqual(view.concentration_nm, 20000)
        self.assertEqual(view.desired_concentration_nm.get(), "5")
        view.desired_concentration_nm.set("250")
        view.update_percentage_options(25)
        self.assertEqual(list(view._fraction_options), ["0%", "20%", "40%", "60%", "80%", "100%"])
        self.assertTrue(any(view.selectable.values()))
        view.desired_concentration_nm.set("5")
        view.update_percentage_options(50)
        self.assertEqual(list(view._fraction_options), ["100%"])
        self.assertEqual(view.replacement_error.get(), "")
        view.update_percentage_options(500)
        self.assertEqual(list(view._fraction_options), ["0%", "50%", "100%"])

    def test_switching_back_reuses_graphic_and_preserves_selection(self):
        view = self.view
        first_label = view.panel_var.get()
        name = next(name for name in view.circle_sites if view.selectable[name])
        canvas, ring, _, _ = view.circle_sites[name]
        view.variables[name].set(True)
        view._on_square_toggled(name)
        view.panel_var.set(str(view.panels[1]["label"]))
        view._show_panel()
        view.mark_conflicts({name})
        with patch.object(view, "_build_panel", wraps=view._build_panel) as build:
            view.panel_var.set(first_label)
            view._show_panel()
            build.assert_not_called()
        self.assertIs(view.circle_sites[name][0], canvas)
        self.assertTrue(view.variables[name].get())
        self.assertEqual(canvas.itemcget(ring, "outline"), view.CONFLICT_BG)
        view.mark_conflicts(set())
        self.assertEqual(canvas.itemcget(ring, "outline"), "#00ffff")
        view.clear()
        self.assertEqual(view.selected(), [])
        self.assertEqual(canvas.itemcget(ring, "fill"), "")

    def test_repaint_counts_selections_once_and_skips_unchanged_conflicts(self):
        view = self.view
        with patch.object(view, "_update_selection_summary", wraps=view._update_selection_summary) as summary:
            view._show_panel()
            summary.assert_called_once()
        with patch.object(view, "_paint_square", wraps=view._paint_square) as paint:
            view.mark_conflicts(set())
            paint.assert_not_called()

    def test_radius_and_sheet_refresh_invalidate_cached_graphics(self):
        view = self.view
        name = next(iter(view.circle_sites))
        canvas = view.circle_sites[name][0]
        view.circle_radius_var.set("3")
        self.assertFalse(canvas.winfo_exists())
        self.assertEqual(view.circle_radius_nm, 3)
        replacement_canvas = view.circle_sites[name][0]
        view.refresh()
        self.assertFalse(replacement_canvas.winfo_exists())
        self.assertEqual(len(view._panel_cache), 1)

    def test_percentages_and_colors_are_locked_per_selection(self):
        view = self.view
        view.desired_concentration_nm.set("50")
        view.update_percentage_options(100)
        names = [name for name in view.circle_sites if view.selectable[name]][:2]
        colors = []
        for name, percentage in zip(names, ("25%", "75%")):
            view.replacement_percentage.set(percentage)
            view.variables[name].set(True)
            view._on_square_toggled(name)
            canvas, ring, _, _ = view.circle_sites[name]
            colors.append(canvas.itemcget(ring, "fill"))
        self.assertNotEqual(*colors)
        view.replacement_percentage.set("100%")
        panels = PicklistApp._replacement_selection_panels(SimpleNamespace(set_views=[view]))
        self.assertEqual(panels[0]["site_fractions"], dict(zip(names, (0.25, 0.75))))
        view._show_panel()
        for name, fraction, color in zip(names, (0.25, 0.75), colors):
            self.assertEqual(view.site_fractions[name], fraction)
            canvas, ring, _, _ = view.circle_sites[name]
            self.assertEqual(canvas.itemcget(ring, "fill"), color)
        view.circle_radius_var.set("3")
        canvas, ring, _, _ = view.circle_sites[names[0]]
        self.assertEqual(canvas.itemcget(ring, "fill"), colors[0])
        view.variables[names[0]].set(False)
        view._on_square_toggled(names[0])
        view.variables[names[0]].set(True)
        view._on_square_toggled(names[0])
        self.assertEqual(view.site_fractions[names[0]], 1)
        view.update_percentage_options(25)
        self.assertEqual(view.site_fractions[names[1]], 0.75)
        self.assertIn("do not fit", view.replacement_error.get())
        view.clear()
        self.assertEqual(view.site_fractions, {})


if __name__ == "__main__":
    unittest.main()
