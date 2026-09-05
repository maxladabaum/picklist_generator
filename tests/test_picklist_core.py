import tempfile
import unittest
import json
import struct
import zlib
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
from selection_report import _grid_metrics, write_replacement_selection_pdf, write_stored_runs_selection_pdf
from template_generator import (
    EXTENSION_COLUMN_SPACING_NM,
    EXTENSION_ROW_SPACING_NM,
    logical_stroke_schema,
    logical_template_sites,
    normalize_logical_bit_schema,
    render_barcode_template,
    save_barcode_template,
    template_size_nm,
)
from picklist_app import COLS, panel_definitions, panel_display_columns, panel_template_positions, panel_sequence_name, soyeon_mb_panels


ROOT = Path(__file__).resolve().parents[1]
SHEETS = ROOT / "replacement_sheets"


class PicklistCoreTests(unittest.TestCase):
    def test_soyeon_panels_expose_all_supplied_extensions(self):
        records, _ = parse_source2(SHEETS / "soyeon_MB_replace.csv")
        panels = soyeon_mb_panels()
        self.assertEqual([len(panel["active"]) for panel in panels], [60, 6])
        names = {
            panel_sequence_name(panel, row, column)
            for panel in panels for row, column in panel["active"]
        }
        self.assertEqual(names, {record["Name"] for record in records})
        self.assertEqual(panel_template_positions(panels[0], {"D_MB03_01_[C08]"}), [(1, 0)])

    def test_soyeon_replaces_all_targets_including_relocated_staples(self):
        records, _ = parse_source2(SHEETS / "soyeon_MB_replace.csv")
        by_name = {row["Name"]: row for row in records}
        for name, target in (("D_MB07_09_[B21]", "B11"), ("D_MB07_12_[B22]", "B14"), ("DL_00_12_[B20]", "L14")):
            self.assertEqual(by_name[name]["Replace Well"], target)
        for hinge in ("original", "balanced_rigid"):
            rows = generate_picklist(
                SHEETS / "book_base.csv",
                [ReplacementSelection(SHEETS / "soyeon_MB_replace.csv", list(by_name), "SoyeonMB[1]")],
                ["A01", "A02"], hinge_type=hinge,
            )
            self.assertEqual(len(rows), 252)
            new = [row for row in rows if row["Source Plate Name"] == "SoyeonMB[1]"]
            self.assertEqual(len(new), 66)
            self.assertEqual({(row["Source Well"], row["Sample Comments"]) for row in new},
                             {(row["Well"], row["Sequence"]) for row in records})
            base, plate = parse_source1(SHEETS / "book_base.csv", hinge)
            targets = {record["Replace Well"] for record in records}
            self.assertEqual({row["Source Well"] for row in rows if row["Source Plate Name"] == plate},
                             {row["Well"] for row in base if row["Replacement Well"] not in targets})

    def test_soyeon_conflicts_with_existing_mb_at_the_same_site(self):
        with self.assertRaisesRegex(ValueError, "Duplicate replacement for O04"):
            generate_picklist(SHEETS / "book_base.csv", [
                ReplacementSelection(SHEETS / "soyeon_MB_replace.csv", ["D_MB05_04_[O04]"], "SoyeonMB[1]"),
                ReplacementSelection(SHEETS / "max_replace_MB.csv", ["D-MB_H05-04"], "SourcePlate4[4]"),
            ], ["A01", "A02"])

    def test_hinge_options_are_mutually_exclusive_and_preserve_other_staples(self):
        rigid_wells = {f"{row}{column}" for row in "FGH" for column in range(19, 25)}
        original_wells = set("I01 L16 J01 G16 C02 K01 L01 J16 M01 N01 K16 O01 H16 D02 P01 A02 I16 B02".split())
        results = {}
        for hinge_type, included, excluded in (
            ("original", original_wells, rigid_wells),
            ("balanced_rigid", rigid_wells, original_wells),
        ):
            rows = generate_picklist(SHEETS / "book_base.csv", [], ["A01", "A02"], hinge_type=hinge_type)
            wells = {row["Source Well"] for row in rows}
            self.assertEqual(len(rows), 252)
            self.assertTrue(included.issubset(wells))
            self.assertTrue(excluded.isdisjoint(wells))
            results[hinge_type] = wells - included
        self.assertEqual(results["original"], results["balanced_rigid"])
        self.assertEqual(
            generate_picklist(SHEETS / "book_base.csv", [], ["A01", "A02"]),
            generate_picklist(SHEETS / "book_base.csv", [], ["A01", "A02"], hinge_type="original"),
        )

    def test_replacement_targets_selected_hinge_at_its_logical_site(self):
        with tempfile.TemporaryDirectory() as folder:
            replacement = Path(folder) / "replacement.csv"
            replacement.write_text("Well,Name,Sequence,Replace Well\nA01,hinge replacement,ACGT,I01\n")
            for hinge_type in ("original", "balanced_rigid"):
                rows = generate_picklist(
                    SHEETS / "book_base.csv",
                    [ReplacementSelection(replacement, ["hinge replacement"], "Replacement")],
                    ["A01", "A02"], hinge_type=hinge_type,
                )
                self.assertEqual(len(rows), 252)
                self.assertFalse(any(row["Source Well"] in {"I01", "F19"} for row in rows))
                self.assertEqual(sum(row["Source Plate Name"] == "Replacement" for row in rows), 1)

    def test_invalid_hinge_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown hinge type"):
            generate_picklist(SHEETS / "book_base.csv", [], ["A01", "A02"], hinge_type="both")

    def test_base_without_hinge_metadata_still_parses(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "base.csv"
            base.write_text("Well Position,Sequence Name,Sequence\nA01,staple,ACGT\n")
            for hinge_type in ("original", "balanced_rigid"):
                rows = generate_picklist(base, [], ["A01"], hinge_type=hinge_type)
                self.assertEqual([row["Source Well"] for row in rows], ["A01"])

    def test_up_extension_panels_use_mirrored_physical_column_order(self):
        panels_by_label = {
            panel["label"]: panel
            for _group, _filename, _plate, panels in panel_definitions()
            for panel in panels
        }
        self.assertEqual(panel_display_columns(panels_by_label["D-Apt"]), COLS)
        self.assertEqual(panel_display_columns(panels_by_label["U-Apt"]), list(reversed(COLS)))
        self.assertEqual(panel_display_columns(panels_by_label["D-MB"]), COLS)
        self.assertEqual(panel_display_columns(panels_by_label["U-MB"]), list(reversed(COLS)))
        self.assertEqual(panel_display_columns(panels_by_label["U-PAINT"]), list(reversed(COLS)))
        self.assertEqual(panel_display_columns(panels_by_label["U-Apt R1"]), list(reversed(COLS)))
        column_pitch, row_pitch, _width, _height = _grid_metrics(panels_by_label["U-Apt R1"])
        self.assertAlmostEqual(panels_by_label["U-Apt R1"]["spacing_x_nm"], 120.0 / 11.0)
        self.assertAlmostEqual(panels_by_label["U-Apt R1"]["spacing_y_nm"], 35.0 / 7.0)
        self.assertAlmostEqual(column_pitch / row_pitch, (120.0 / 11.0) / (35.0 / 7.0))

    def test_up_extension_template_uses_mirrored_view_coordinates(self):
        panels_by_label = {
            panel["label"]: panel
            for _group, _filename, _plate, panels in panel_definitions()
            for panel in panels
        }
        down = panels_by_label["D-Apt"]
        up = panels_by_label["U-Apt"]

        self.assertEqual(
            panel_template_positions(down, {"D-Apt_H01-01"}),
            [(0, 0)],
        )
        self.assertEqual(
            panel_template_positions(up, {"U-Apt_H01-01"}),
            [(0, 11)],
        )
        self.assertEqual(
            panel_template_positions(up, {"U-Apt_H01-12"}),
            [(0, 0)],
        )

    def test_default_origami_template_has_expected_footprint_and_sites(self):
        width_nm, height_nm = template_size_nm(
            8,
            12,
            EXTENSION_COLUMN_SPACING_NM,
            EXTENSION_ROW_SPACING_NM,
            20.0,
        )
        self.assertAlmostEqual(width_nm, 160.0)
        self.assertAlmostEqual(height_nm, 75.0)
        width, height, pixels, metadata = render_barcode_template(
            8, 12, {(0, 0), (3, 6), (7, 11)}
        )
        self.assertEqual((width, height), (500, 234))
        self.assertEqual(len(pixels), width * height)
        self.assertEqual(metadata["selected_site_count"], 3)
        self.assertAlmostEqual(metadata["width_nm"], 160.0)
        self.assertAlmostEqual(metadata["height_nm"], 75.0)
        self.assertAlmostEqual(metadata["pixel_size_x_nm"], 160.0 / 499.0)
        self.assertAlmostEqual(metadata["pixel_size_y_nm"], 75.0 / 233.0)
        self.assertGreaterEqual(max(pixels), 250)
        self.assertEqual(pixels[0], 0)

    def test_logical_strokes_expand_to_alignment_anchors_and_multi_site_bits(self):
        schema = logical_stroke_schema(8, 12)
        self.assertIsNotNone(schema)
        self.assertEqual(len(schema["alignment_strokes"]), 4)
        self.assertEqual(len(schema["logical_bits"]), 8)
        selected = set(logical_template_sites(8, 12, {"left.slash", "right.bottom"}))
        self.assertTrue(all((0, column) in selected for column in range(12)))
        self.assertTrue(all((row, 0) in selected and (row, 6) in selected for row in range(8)))
        self.assertIn((3, 4), selected)
        self.assertIn((7, 8), selected)
        self.assertNotIn((3, 5), selected)

    def test_template_metadata_records_logical_bit_states(self):
        active = {"left.slash", "right.right"}
        selected = logical_template_sites(8, 12, active)
        _width, _height, _pixels, metadata = render_barcode_template(
            8,
            12,
            selected,
            active_logical_bits=active,
            logical_schema=logical_stroke_schema(8, 12),
        )
        model = metadata["logical_model"]
        self.assertEqual(model["format"], "paint-analysis-logical-bits-v1")
        self.assertEqual(set(model["active_logical_bits"]), active)

    def test_established_logical_patterns_expand_to_expected_physical_counts(self):
        all_bits = [
            f"{panel}.{stroke}"
            for panel in ("left", "right")
            for stroke in ("slash", "backslash", "right", "bottom")
        ]
        diagonals = [bit for bit in all_bits if bit.endswith(("slash", "backslash"))]
        square = [bit for bit in all_bits if bit.endswith(("right", "bottom"))]
        self.assertEqual(len(logical_template_sites(8, 12, ())), 26)
        self.assertEqual(len(logical_template_sites(8, 12, diagonals)), 46)
        self.assertEqual(len(logical_template_sites(8, 12, square)), 48)
        self.assertEqual(len(logical_template_sites(8, 12, all_bits)), 64)

    def test_arbitrary_logical_bit_schema_is_rendered_and_embedded(self):
        schema = {
            "format": "paint-analysis-logical-bits-v1",
            "physical_rows": 3,
            "physical_columns": 4,
            "description": "Non-stroke test encoding",
            "alignment_groups": [
                {"id": "fiducial", "label": "Anchor", "physical_sites": [[1, 1], [3, 4]]}
            ],
            "logical_bits": [
                {
                    "id": "checker",
                    "label": "Checker bit",
                    "physical_sites": [[1, 2], [2, 3], [3, 2]],
                    "evidence_sites": [[1, 2], [3, 2]],
                },
                {"id": "block", "physical_sites": [[2, 1], [2, 2]]},
            ],
        }
        normalized = normalize_logical_bit_schema(schema, 3, 4)
        selected = logical_template_sites(3, 4, {"checker"}, schema=normalized)
        self.assertEqual(set(selected), {(0, 0), (2, 3), (0, 1), (1, 2), (2, 1)})
        width, height, pixels, metadata = render_barcode_template(
            3,
            4,
            selected,
            spacing_x_nm=10.0,
            spacing_y_nm=10.0,
            active_logical_bits={"checker"},
            logical_schema=normalized,
        )
        self.assertEqual(metadata["logical_model"]["active_logical_bits"], ["checker"])
        self.assertEqual(metadata["logical_model"]["logical_bits"][1]["evidence_sites"], [[2, 1], [2, 2]])
        self.assertEqual(len(pixels), width * height * 3)
        self.assertEqual(metadata["png_channels"], "RGB group colors")
        colors = [bit["display_color"] for bit in metadata["logical_model"]["logical_bits"]]
        self.assertEqual(len(set(colors)), 2)

    def test_custom_logical_groups_may_share_physical_sites(self):
        schema = {
            "format": "paint-analysis-logical-bits-v1",
            "physical_rows": 2,
            "physical_columns": 3,
            "alignment_groups": [],
            "logical_bits": [
                {"id": "bit_a", "physical_sites": [[1, 1], [1, 2]]},
                {"id": "bit_b", "physical_sites": [[1, 2], [1, 3]]},
            ],
        }
        normalized = normalize_logical_bit_schema(schema, 2, 3)
        selected = logical_template_sites(2, 3, {"bit_a", "bit_b"}, schema=normalized)
        self.assertEqual(set(selected), {(0, 0), (0, 1), (0, 2)})
        self.assertIn([1, 2], normalized["logical_bits"][0]["physical_sites"])
        self.assertIn([1, 2], normalized["logical_bits"][1]["physical_sites"])

    def test_saved_origami_template_is_valid_grayscale_png_with_json_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            png_path, json_path, metadata = save_barcode_template(
                Path(folder) / "barcode",
                3,
                4,
                {(0, 1), (2, 2)},
                spacing_x_nm=20.0,
                spacing_y_nm=20.0,
                width_px=100,
            )
            data = png_path.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            ihdr_length = struct.unpack(">I", data[8:12])[0]
            self.assertEqual(ihdr_length, 13)
            self.assertEqual(data[12:16], b"IHDR")
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
            self.assertEqual((width, height, bit_depth, color_type), (100, 80, 8, 0))
            offset = 8
            compressed = bytearray()
            embedded_metadata = None
            while offset < len(data):
                length = struct.unpack(">I", data[offset : offset + 4])[0]
                kind = data[offset + 4 : offset + 8]
                payload = data[offset + 8 : offset + 8 + length]
                if kind == b"IDAT":
                    compressed.extend(payload)
                elif kind == b"tEXt" and payload.startswith(b"paint_analysis_template\x00"):
                    embedded_metadata = json.loads(payload.split(b"\x00", 1)[1].decode("utf-8"))
                offset += 12 + length
            raw = zlib.decompress(bytes(compressed))
            self.assertEqual(len(raw), height * (width + 1))
            self.assertTrue(all(raw[row * (width + 1)] == 0 for row in range(height)))
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), metadata)
            self.assertEqual(embedded_metadata, metadata)
            self.assertAlmostEqual(metadata["pixel_size_x_nm"], 100.0 / 99.0)
            self.assertAlmostEqual(metadata["pixel_size_y_nm"], 80.0 / 79.0)

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
