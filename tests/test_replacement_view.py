"""Regression checks for replacement panel reuse and selection updates."""

import tkinter as tk
import unittest
from unittest.mock import patch

from picklist_app import SHEET_DIR, PicklistApp, SetView, panel_definitions
from types import SimpleNamespace


class ReplacementViewTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        title, filename, plate, panels = panel_definitions()[0]
        self.view = SetView(self.root, title, SHEET_DIR / filename, plate, panels)

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
        view.excess_multiplier.set("1")
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
