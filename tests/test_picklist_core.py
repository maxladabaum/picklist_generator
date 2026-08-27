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
    generate_picklist,
    parse_source1,
    parse_source2,
)


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
