import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app_state import (
    clear_plate,
    create_run_output_directory,
    next_unused_wells,
    plate_wells,
    record_transfers,
    unused_wells_from,
)
from picklist_core import (
    ReplacementSelection,
    calculate_mixing_volumes,
    combine_picklists,
    generate_picklist,
    parse_source1,
    parse_source2,
    separate_mixing_recipes,
)
from selection_report import write_replacement_selection_pdf, write_stored_runs_selection_pdf


ROOT = Path(__file__).resolve().parents[1]
SHEETS = ROOT / "replacement_sheets"


class PicklistCoreTests(unittest.TestCase):
    def test_all_bundled_sheets_parse(self):
        base, _ = parse_source1(SHEETS / "book_base.csv")
        self.assertGreater(len(base), 250)
        for filename in (
            "yaritza_replace.csv", "samuel_replace.csv", "max_replace.csv",
            "max_replace_MB.csv", "PAINT_replace.csv", "PAINT_R1_replace.csv",
        ):
            rows, _ = parse_source2(SHEETS / filename)
            self.assertTrue(rows, filename)

    def test_paint_r1_row_15_uses_its_distinct_replacement_scheme(self):
        base, _ = parse_source1(SHEETS / "book_base.csv")
        rows = generate_picklist(
            SHEETS / "book_base.csv",
            [
                ReplacementSelection(
                    SHEETS / "PAINT_R1_replace.csv",
                    ["U-Apt_H15-01_R1"],
                    "SourcePlate6[6]",
                )
            ],
            ["A01", "A02"],
        )
        self.assertEqual(len(rows), len(base))
        self.assertTrue(
            any(
                row["Source Plate Name"] == "SourcePlate6[6]"
                and row["Source Well"] == "O2"
                for row in rows
            )
        )
        self.assertFalse(
            any(
                row["Source Plate Name"] == "SourcePlate1[1]"
                and row["Source Well"] == "H09"
                for row in rows
            )
        )

    def test_replacement_preserves_active_source_count(self):
        base, _ = parse_source1(SHEETS / "book_base.csv")
        rows = generate_picklist(
            SHEETS / "book_base.csv",
            [ReplacementSelection(SHEETS / "yaritza_replace.csv", ["D-Apt_H01-01"], "SourcePlate3[3]")],
            ["A01", "A02"],
        )
        self.assertEqual(len(rows), len(base))
        self.assertTrue(any(row["Source Plate Name"] == "SourcePlate3[3]" for row in rows))
        self.assertFalse(any(row["Source Plate Name"] == "SourcePlate1[1]" and row["Source Well"] == "J08" for row in rows))

    def test_relocated_base_staples_use_new_wells_and_old_replacement_keys(self):
        relocations = dict(zip(
            "L11 L14 B11 B14 K08 K05 A08 A05 H05 H08 H11 H14 I05 I08 I11 I14".split(),
            "B19 B20 B21 B22 C19 C20 C21 C22 D17 D18 D19 D20 D21 D22 D23 D24".split(),
        ))
        base, _ = parse_source1(SHEETS / "book_base.csv")
        by_key = {row["Replacement Well"]: row["Well"] for row in base}
        self.assertEqual({old: by_key[old] for old in relocations}, relocations)

        rows = generate_picklist(SHEETS / "book_base.csv", [], ["A01", "A02"])
        base_wells = {
            row["Source Well"] for row in rows if row["Source Plate Name"] == "SourcePlate1[1]"
        }
        self.assertTrue(set(relocations.values()).issubset(base_wells))
        self.assertTrue(set(relocations).isdisjoint(base_wells))

        with tempfile.TemporaryDirectory() as folder:
            replacement = Path(folder) / "replacement.csv"
            replacement.write_text(
                "Well,Name,Sequence,Replace Well\nA01,R1 PAINT example,ACGT,L14\n",
                encoding="utf-8",
            )
            replaced = generate_picklist(
                SHEETS / "book_base.csv",
                [ReplacementSelection(replacement, ["R1 PAINT example"], "ReplacementPlate[1]")],
                ["A01", "A02"],
            )
        self.assertFalse(
            any(
                row["Source Plate Name"] == "SourcePlate1[1]" and row["Source Well"] == "B20"
                for row in replaced
            )
        )
        self.assertTrue(
            any(
                row["Source Plate Name"] == "ReplacementPlate[1]" and row["Source Well"] == "A01"
                for row in replaced
            )
        )

    def test_duplicate_replacement_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate replacement"):
            generate_picklist(
                SHEETS / "book_base.csv",
                [ReplacementSelection(SHEETS / "max_replace.csv", ["PDGF-14_Top-R3", "PDGF-18_Top-R3"], "SourcePlate4[4]")],
                ["A01", "A02"],
            )

    def test_recipe_sums_to_desired_volume(self):
        recipe = calculate_mixing_volumes(
            [200000, 1, 0, 0], 281, [400, 0.1, 0, 0], [0, 0, 100, 0],
            [0, 0, 0, 100], [0, 10, 0, 0], [500, 10, 1, 1, 12, 5],
        )
        self.assertAlmostEqual(sum(row["Volume_uL"] for row in recipe), 500.0)

    def test_stored_picklists_combine_and_recipes_remain_separate(self):
        first = [{"Destination Plate Name": "Destination[1]", "Destination Well": "A01", "Transfer Volume": 50}]
        second = [{"Destination Plate Name": "Destination[1]", "Destination Well": "A02", "Transfer Volume": 50}]
        self.assertEqual(combine_picklists([first, second]), first + second)
        recipes = separate_mixing_recipes([
            [{"Reagent": "Staple", "Volume_uL": 1.25}, {"Reagent": "Water", "Volume_uL": 8.75}],
            [{"Reagent": "Staple", "Volume_uL": 2.5}, {"Reagent": "Water", "Volume_uL": 17.5}],
        ])
        self.assertEqual(recipes, [
            [{"Reagent": "Staple", "Volume_uL": 1.25}, {"Reagent": "Water", "Volume_uL": 8.75}],
            [{"Reagent": "Staple", "Volume_uL": 2.5}, {"Reagent": "Water", "Volume_uL": 17.5}],
        ])

    def test_combining_picklists_rejects_overlapping_destination_wells(self):
        row = {"Destination Plate Name": "Destination[1]", "Destination Well": "A01", "Transfer Volume": 50}
        with self.assertRaisesRegex(ValueError, "both use destination"):
            combine_picklists([[row], [dict(row)]])

    def test_replacement_selection_pdf_contains_used_panels(self):
        panels = [{
            "group": "Aptamers",
            "label": "PDGF & Apt",
            "rows": ["Top", "Mid"],
            "columns": ["L3", "R3"],
            "active": [("Top", "L3"), ("Top", "R3"), ("Mid", "L3"), ("Mid", "R3")],
            "selected": [("Top", "R3")],
            "selected_names": ["PDGF-14_Top-R3"],
        }]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "selections.pdf"
            write_replacement_selection_pdf(output, panels, datetime(2026, 8, 27, 16, 30, 0))
            data = output.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertTrue(data.endswith(b"%%EOF\n"))
            self.assertIn(b"PDGF & Apt", data)
            self.assertIn(b"PDGF-14_Top-R3", data)

    def test_stored_run_selection_pdf_separates_runs(self):
        panel = {
            "group": "Aptamers", "label": "PDGF-14",
            "rows": ["Top"], "columns": ["R3"],
            "active": [("Top", "R3")], "selected": [("Top", "R3")],
            "selected_names": ["PDGF-14_Top-R3"],
        }
        runs = [
            {"run_name": "Design alpha", "panels": [panel]},
            {"run_name": "Design beta", "panels": [panel]},
        ]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "stored-runs.pdf"
            write_stored_runs_selection_pdf(output, runs, datetime(2026, 8, 27, 16, 30, 0))
            data = output.read_bytes()
            self.assertIn(b"/Count 2", data)
            self.assertIn(b"Run: Design alpha", data)
            self.assertIn(b"Run: Design beta", data)

    def test_destination_state_records_usage_and_skips_used_wells(self):
        state = {"version": 1, "plates": {}}
        record_transfers(
            state,
            [
                {"Destination Plate Name": "Destination[1]", "Destination Well": "A01", "Transfer Volume": 50},
                {"Destination Plate Name": "Destination[1]", "Destination Well": "A01", "Transfer Volume": 50},
                {"Destination Plate Name": "Destination[1]", "Destination Well": "A02", "Transfer Volume": 25},
            ],
        )
        wells = plate_wells(state, "Destination[1]")
        self.assertEqual(wells["A01"]["volume_nL"], 100)
        self.assertEqual(wells["A01"]["transfer_count"], 2)
        self.assertEqual(next_unused_wells(state, "Destination[1]", 3), ["A03", "A04", "A05"])

    def test_each_output_directory_is_timestamped_and_unique(self):
        with tempfile.TemporaryDirectory() as folder:
            when = datetime(2026, 8, 10, 14, 32, 7)
            first = create_run_output_directory(Path(folder), when)
            second = create_run_output_directory(Path(folder), when)
            self.assertEqual(first.name, "2026-08-10_14-32-07")
            self.assertEqual(second.name, "2026-08-10_14-32-07_02")

    def test_starting_well_override_and_fresh_plate(self):
        state = {"version": 1, "plates": {"Destination[1]": {"wells": {
            "A01": {"volume_nL": 50}, "C02": {"volume_nL": 50}
        }}}}
        self.assertEqual(
            unused_wells_from(state, "Destination[1]", "C01", 3),
            ["C01", "C03", "C04"],
        )
        with self.assertRaisesRegex(ValueError, "already recorded as used"):
            unused_wells_from(state, "Destination[1]", "C02", 2)
        self.assertTrue(clear_plate(state, "Destination[1]"))
        self.assertEqual(next_unused_wells(state, "Destination[1]", 2), ["A01", "A02"])


if __name__ == "__main__":
    unittest.main()
