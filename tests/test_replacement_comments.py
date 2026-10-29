import unittest
from pathlib import Path

from picklist_core import ReplacementSelection, generate_picklist, parse_source2


SHEETS = Path(__file__).resolve().parents[1] / "replacement_sheets"


class ReplacementCommentTests(unittest.TestCase):
    def test_comments_identify_each_panel_without_changing_transfers(self):
        names = ["PDGF-Apt_Mid-L1", "PDGF-14_Mid-R1"]
        panels = dict(zip(names, ["PDGF-Apt", "PDGF-14"]))
        plain = ReplacementSelection(SHEETS / "max_replace.csv", names, "SourcePlate4[4]")
        annotated = ReplacementSelection(
            plain.csv_path, names, plain.plate_name,
            replacement_tab="Aptamers", panel_names=panels,
        )
        before = generate_picklist(SHEETS / "book_base.csv", [plain], ["A01", "A02", "A03"])
        after = generate_picklist(SHEETS / "book_base.csv", [annotated], ["A01", "A02", "A03"])
        records, _ = parse_source2(plain.csv_path)
        by_well = {row["Well"].upper(): row for row in records if row["Name"] in names}
        self.assertEqual(len(before), len(after))
        count = 0
        for original, updated in zip(before, after):
            self.assertEqual(
                {k: v for k, v in original.items() if k != "Sample Comments"},
                {k: v for k, v in updated.items() if k != "Sample Comments"},
            )
            if updated["Source Plate Name"] == plain.plate_name:
                record = by_well[updated["Source Well"]]
                self.assertEqual(updated["Sample Comments"],
                    "Tab: Aptamers | Panel: {} | Staple: {} | Sequence: {}".format(
                        panels[record["Name"]], record["Name"], record["Sequence"]))
                count += 1
            else:
                self.assertEqual(original["Sample Comments"], updated["Sample Comments"])
                self.assertTrue(updated["Sample Comments"].startswith("Tab: Base | "))
        self.assertEqual(count, 2)

    def test_only_hinge_staples_are_labeled_with_their_hinge_type(self):
        from picklist_core import parse_source1
        for hinge_type, label in (("original", "Flexible"), ("balanced_rigid", "Rigid")):
            with self.subTest(hinge_type=hinge_type):
                base, _ = parse_source1(SHEETS / "book_base.csv", hinge_type)
                hinges = {row["Well"] for row in base if row.get("Hinge Type")}
                self.assertTrue(hinges)
                transfers = generate_picklist(
                    SHEETS / "book_base.csv", [], ["A01", "A02", "A03"], hinge_type=hinge_type)
                for row in transfers:
                    comment = row["Sample Comments"]
                    if row["Source Well"] in hinges:
                        self.assertIn("Hinge: {} |".format(label), comment)
                    else:
                        self.assertNotIn("Hinge:", comment)
