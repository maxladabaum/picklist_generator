import tempfile
import unittest
from pathlib import Path

from picklist_core import ReplacementSelection, generate_picklist, replacement_fractions
from selection_report import _page_commands


class ReplacementPercentageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name) / "base.csv"
        self.base.write_text("Well Position,Sequence Name,Sequence\nA01,one,AAA\nA02,two,CCC\n")
        self.replacement = Path(folder.name) / "replacement.csv"
        self.replacement.write_text("Well,Name,Sequence,Replace Well\nB01,R1,TTT,A01\nB02,R2,GGG,A02\n")

    def generate(self, fractions, repeats=1):
        return generate_picklist(
            self.base, [ReplacementSelection(self.replacement, ["R1", "R2"], "Replacement",
                                            2, site_fractions=fractions)], ["A01"],
            transfer_volume_nl=50, transfers_per_source=repeats)

    def test_independent_splits_preserve_total_and_repeats(self):
        rows = self.generate({"R1": 0.25, "R2": 0.75}, repeats=2)
        for well, volume in {"A01": 75, "A02": 25, "B01": 25, "B02": 75}.items():
            self.assertEqual([r["Transfer Volume"] for r in rows if r["Source Well"] == well], [volume, volume])
        self.assertEqual(sum(r["Transfer Volume"] for r in rows), 400)

    def test_zero_and_full_do_not_emit_zero_transfers(self):
        rows = self.generate({"R1": 0, "R2": 1})
        self.assertEqual({r["Source Well"]: r["Transfer Volume"] for r in rows}, {"A01": 100, "B02": 100})

    def test_invalid_and_unselected_fractions_rejected(self):
        for fraction in (-1, 1.1, float("nan"), 0.3):
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                self.generate({"R1": fraction})
        with self.assertRaisesRegex(ValueError, "unselected"):
            self.generate({"unknown": 0.5})

    def test_options_follow_excess_adjusted_volume(self):
        self.assertEqual(replacement_fractions(25, 5), [0, 0.2, 0.4, 0.6, 0.8, 1])

    def test_report_keeps_individual_percentages(self):
        panel = {"rows": ["Top"], "columns": ["1", "2"], "selected": [("Top", "1"), ("Top", "2")],
                 "selected_names": ["R1", "R2"], "site_fractions": {"R1": 0.25, "R2": 0.75}}
        commands = _page_commands([(panel, 24, 76, 180)], 1, 1, 2, "today")
        self.assertIn(b"25%", commands)
        self.assertIn(b"75%", commands)
        self.assertIn(b"0.802 0.915 0.840 rg", commands)
        self.assertIn(b"0.407 0.745 0.520 rg", commands)
