import tempfile
import unittest
from pathlib import Path

from picklist_core import ReplacementSelection, generate_picklist, replacement_fractions
from selection_report import _page_commands


class ReplacementPercentageTests(unittest.TestCase):
    def test_individual_concentrations_on_same_plate(self):
        rows = generate_picklist(self.base, [ReplacementSelection(
            self.replacement, ["R1", "R2"], "Replacement", desired_concentration_nm=50,
            site_concentrations_nm={"R1": 100, "R2": 25},
            site_fractions={"R1": 0.5},
        )], ["A01"], transfer_volume_nl=50)
        self.assertEqual({row["Source Well"]: row["Transfer Volume"] for row in rows},
                         {"A01": 50, "B01": 50, "B02": 25})
        for overrides in ({"R1": 0}, {"R1": float("nan")}, {"unknown": 100}, {"R1": 30}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                generate_picklist(self.base, [ReplacementSelection(
                    self.replacement, ["R1"], "Replacement", site_concentrations_nm=overrides,
                )], ["A01"])

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
                                            100, site_fractions=fractions)], ["A01"],
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

    def test_options_follow_desired_concentration(self):
        self.assertEqual(replacement_fractions(25, 250), [0, 0.2, 0.4, 0.6, 0.8, 1])

    def test_mixed_concentrations_preserve_molar_splits(self):
        rows = generate_picklist(
            self.base, [
                ReplacementSelection(self.replacement, ["R1"], "LowConc", 250,
                                     site_fractions={"R1": 0.4}, concentration_nm=20000),
                ReplacementSelection(self.replacement, ["R2"], "Standard"),
            ], ["A01"], transfer_volume_nl=25,
        )
        volumes = {r["Source Well"]: r["Transfer Volume"] for r in rows}
        self.assertEqual(volumes, {"A01": 75, "B01": 500, "B02": 25})
        replacement_amount = volumes["B01"] * 20000
        original_amount = volumes["A01"] * 200000
        self.assertAlmostEqual(replacement_amount / (replacement_amount + original_amount), 0.4)

    def test_low_concentration_capacity_and_droplet_checks(self):
        selection = ReplacementSelection(self.replacement, ["R1"], "LowConc", concentration_nm=20000)
        rows = generate_picklist(self.base, [selection], ["A01", "A02"],
                                 transfer_volume_nl=50, max_destination_volume_ul=0.5)
        self.assertEqual([r["Destination Well"] for r in rows], ["A01", "A02"])
        with self.assertRaisesRegex(ValueError, "capacity"):
            generate_picklist(self.base, [selection], ["A01"], max_destination_volume_ul=0.4)
        with self.assertRaisesRegex(ValueError, "25 nL"):
            generate_picklist(self.base, [selection], ["A01"], base_concentration_nm=205000)

    def test_invalid_stock_concentrations(self):
        for concentration in (0, -1, float("nan"), float("inf")):
            with self.subTest(concentration=concentration):
                selection = ReplacementSelection(self.replacement, ["R1"], "LowConc",
                                                 concentration_nm=concentration)
                with self.assertRaisesRegex(ValueError, "concentration"):
                    generate_picklist(self.base, [selection], ["A01"])
                with self.assertRaisesRegex(ValueError, "concentration"):
                    generate_picklist(self.base, [], ["A01"], base_concentration_nm=concentration)

    def test_percentage_options_check_both_stock_volumes(self):
        self.assertEqual(replacement_fractions(25, 250, concentration_ratio=10), [0, 0.2, 0.4, 0.6, 0.8, 1])
        self.assertEqual(replacement_fractions(100, 50, concentration_ratio=0.5), [0, 0.5, 1])
        self.assertEqual(replacement_fractions(50, 5, concentration_ratio=10), [1])
        self.assertEqual(replacement_fractions(500, 5, concentration_ratio=10), [0, 0.5, 1])

    def test_invalid_final_concentration_targets(self):
        for target in (0, -1, float("nan"), float("inf")):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "concentration"):
                generate_picklist(self.base, [ReplacementSelection(
                    self.replacement, ["R1"], "LowConc", desired_concentration_nm=target,
                )], ["A01"])

    def test_unachievable_target_rejected_instead_of_rounded(self):
        with self.assertRaisesRegex(ValueError, "25 nL"):
            generate_picklist(self.base, [ReplacementSelection(
                self.replacement, ["R1"], "LowConc", concentration_nm=20000,
                desired_concentration_nm=6,
            )], ["A01"], transfer_volume_nl=50, base_desired_concentration_nm=50)

    def test_report_keeps_individual_percentages(self):
        panel = {"rows": ["Top"], "columns": ["1", "2"], "selected": [("Top", "1"), ("Top", "2")],
                 "selected_names": ["R1", "R2"], "site_fractions": {"R1": 0.25, "R2": 0.75}}
        commands = _page_commands([(panel, 24, 76, 180)], 1, 1, 2, "today")
        self.assertIn(b"25%", commands)
        self.assertIn(b"75%", commands)
        self.assertIn(b"0.802 0.915 0.840 rg", commands)
        self.assertIn(b"0.407 0.745 0.520 rg", commands)
