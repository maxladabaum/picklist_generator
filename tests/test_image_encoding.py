import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import numpy as np
from gaussian_reconstruction import render_gaussian_overlay

from app_state import all_384_wells, load_json
from image_encoding import (
    BIT_SITES, CHANNELS, COLOR_BITS, FIT_MODES, PIXEL_X_NM, PIXEL_Y_NM,
    DEFAULT_SCHEME, ENCODING_SCHEMES, barcode_bits, physical_raster_size, rotate_point, rotate_pixels,
    bin_image, compile_designs, geometry, prepare_runs, reconstruct, site_name, physical_reference_raster,
)
from picklist_core import ReplacementSelection, generate_picklist, parse_source1, parse_source2

ROOT = Path(__file__).resolve().parents[1]
SHEETS = ROOT / "replacement_sheets"
SETTINGS = dict(source_plate_type="384PP_AQ_BP", destination_plate_name="Image plate",
                max_destination_volume_ul=12.5, transfers_per_source=1, hinge_type="original")
MIXING = dict(staple=[200000, 1, 0, 0], scaffold=[400, .1, 0, 0], magnesium=[0, 0, 100, 0],
              sodium=[0, 0, 0, 100], te_10x=[0, 10, 0, 0], desired=[100, 50, 10, 1, 12, 5])


def solid(rgb):
    return [[list(rgb) for _ in range(24)] for _ in range(32)]


def designs(pixels=None, base=50, excess=5):
    return compile_designs(pixels if pixels is not None else solid((255, 128, 0)), base, {color: excess for color in CHANNELS})


def prepare(items, available=None, **settings):
    return prepare_runs(items, SHEETS / "book_base.csv",
                        {color: (SHEETS / "PAINT_R1_replace.csv", "SourcePlate6[6]") for color in CHANNELS},
                        available if available is not None else all_384_wells(), {**SETTINGS, **settings}, MIXING)


class ImageEncodingTests(unittest.TestCase):
    def test_each_pixel_channel_occurs_once_with_correct_nm_and_orientation(self):
        pixels = [[[x * 10, y * 8, (x + y) % 256] for x in range(24)] for y in range(32)]
        items = designs(pixels)
        seen = set()
        self.assertEqual(len(items), 48)
        for design in items:
            self.assertEqual(len(design["image_sites"]), 48)
            for site in design["image_sites"]:
                x, y = site["pixel_x"], site["pixel_y"]
                key = (x, y, design["channel"])
                self.assertNotIn(key, seen)
                seen.add(key)
                self.assertEqual(site["rgb"], pixels[y][x])
                self.assertEqual(x, (6 - int(site["column"])) * 4 + design["x_shift"])
                self.assertEqual(y, ((int(site["row"][1:]) - 1) // 2) * 4 + design["y_shift"])
                self.assertAlmostEqual(site["x_nm"], x * 2.475)
                self.assertAlmostEqual(site["y_nm"], y * 1.275)
        self.assertEqual(len(seen), 24 * 32 * 3)
        self.assertAlmostEqual(geometry()["field_width_nm"], 59.4)
        self.assertAlmostEqual(geometry()["field_height_nm"], 40.8)
        self.assertAlmostEqual(geometry()["image_origin_from_C12_H01_nm"][0], 69.4)

    def test_all_barcode_states_are_exact_and_independent_of_image_intensity(self):
        codes = set()
        for design in designs(solid((0, 0, 0))):
            bits = design["bits"]
            self.assertEqual(tuple(bits[:2]), COLOR_BITS[design["color"]])
            self.assertEqual(2*bits[2] + bits[3], design["x_shift"])
            self.assertEqual(2*bits[4] + bits[5], design["y_shift"])
            codes.add(tuple(bits))
            for bit, sites in zip(bits, BIT_SITES):
                for row, column in sites:
                    self.assertEqual(design["site_fractions"][site_name(row, column)], bit)
            self.assertTrue(all(site["replacement_fraction"] == 0 for site in design["image_sites"]))
        self.assertEqual(len(codes), 48)

    def test_nearest_quantization_preserves_total_volume(self):
        pixels = [[[x*11 % 256, y*9 % 256, (x+y)*5 % 256] for x in range(24)] for y in range(32)]
        for base, excess in ((25, 1), (100, 1), (50, 5), (50, 1.5)):
            with self.subTest(base=base, excess=excess):
                items = designs(pixels, base, excess)
                step = 25 / (base * excess)
                for design in items:
                    for site in design["image_sites"]:
                        self.assertLessEqual(abs(site["replacement_fraction"] - site["channel_value"]/255), step/2 + 1e-9)
                        self.assertEqual(site["original_nl"] + site["replacement_nl"], base*excess)
                        self.assertEqual(site["original_nl"] % 25, 0)
                        self.assertEqual(site["replacement_nl"] % 25, 0)
        self.assertEqual(reconstruct(designs(solid((255, 128, 0)), 100, 1)), solid((255, 128, 0)))

    def test_fitting_uses_physical_aspect_and_transparency(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "square.png"
            Image.new("RGB", (100, 100), "white").save(path)
            fitted = bin_image(path)
            self.assertEqual(fitted[16][0], [0, 0, 0])
            self.assertEqual(fitted[16][12], [255, 255, 255])
            for mode in FIT_MODES[1:]:
                self.assertEqual(bin_image(path, mode), solid((255, 255, 255)))
            Image.new("RGBA", (10, 10), (255, 0, 0, 0)).save(path)
            self.assertEqual(bin_image(path), solid((0, 0, 0)))
            Image.new("RGB", (792, 544), (0, 128, 255)).save(path)
            self.assertEqual(bin_image(path), solid((0, 128, 255)))

    def test_binning_averages_samples_and_respects_exif_orientation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pattern.png"
            image = Image.new("RGB", (792, 544), "black")
            for y in range(544):
                for x in range(0, 792, 3):
                    image.putpixel((x, y), (255, 0, 0))
            image.save(path)
            self.assertEqual(bin_image(path), solid((85, 0, 0)))
            image = Image.new("RGB", (80, 40), "black")
            for y in range(40):
                for x in range(40):
                    image.putpixel((x, y), (255, 0, 0))
            exif = Image.Exif()
            exif[274] = 6  # rotate 90 degrees clockwise
            image.save(path, exif=exif)
            pixels = bin_image(path, FIT_MODES[2])
            self.assertEqual(pixels[2][12], [255, 0, 0])
            self.assertEqual(pixels[29][12], [0, 0, 0])

    def test_batch_wells_never_overlap_and_picklists_preserve_originals(self):
        runs = prepare(designs())
        occupied = set()
        totals = set()
        by_name = {row["Name"]: row for row in parse_source2(SHEETS / "PAINT_R1_replace.csv")[0]}
        for run in runs:
            self.assertFalse(occupied & set(run["destination_wells"]))
            occupied.update(run["destination_wells"])
            total = sum(row["Transfer Volume"] for row in run["picklist"])
            totals.add(total)
            volumes = {}
            for row in run["picklist"]:
                key = (row["Source Plate Name"], row["Source Well"])
                volumes[key] = volumes.get(key, 0) + row["Transfer Volume"]
                self.assertGreater(row["Transfer Volume"], 0)
                self.assertEqual(row["Transfer Volume"] % 25, 0)
            base_rows, base_plate = parse_source1(SHEETS / "book_base.csv")
            original_wells = {row["Replacement Well"]: row["Well"] for row in base_rows}
            for site in run["image_design"]["image_sites"]:
                source = by_name[site["name"]]
                self.assertEqual(volumes.get(("SourcePlate6[6]", source["Well"]), 0), site["replacement_nl"])
                self.assertEqual(volumes.get((base_plate, original_wells[source["Replace Well"]]), 0), site["original_nl"])
            self.assertAlmostEqual(sum(row["Volume_uL"] for row in run["mixing_recipe"]), 100)
            self.assertLessEqual(run["mixing_recipe"][0]["Volume_uL"], total/1000)
        self.assertEqual(len(totals), 1)
        self.assertEqual(len(occupied), 144)

    def test_insufficient_wells_and_invalid_inputs_fail_without_partial_runs(self):
        with self.assertRaisesRegex(ValueError, "destination well"):
            prepare(designs(), ["A01", "A02", "A03"])
        with self.assertRaisesRegex(ValueError, "unique"):
            prepare(designs(), ["A01", "A01"])
        with self.assertRaises(ValueError):
            designs(solid((256, 0, 0)))
        with self.assertRaises(ValueError):
            designs(solid((0, 0, 0))[:20])
        with self.assertRaises(ValueError):
            designs(base=33)

    def test_repeats_and_relocated_hinges_preserve_each_site_split(self):
        item = next(item for item in designs() if item["color"] == "R1 G")
        run = prepare([item], hinge_type="balanced_rigid", transfers_per_source=2)[0]
        base_rows, base_plate = parse_source1(SHEETS / "book_base.csv", "balanced_rigid")
        original_wells = {row["Replacement Well"]: row["Well"] for row in base_rows}
        sources = {row["Name"]: row for row in parse_source2(SHEETS / "PAINT_R1_replace.csv")[0]}
        volumes = {}
        for row in run["picklist"]:
            key = row["Source Plate Name"], row["Source Well"]
            volumes[key] = volumes.get(key, 0) + row["Transfer Volume"]
        for name, fraction in item["site_fractions"].items():
            source = sources[name]
            self.assertAlmostEqual(volumes.get(("SourcePlate6[6]", source["Well"]), 0), 500 * fraction)
            self.assertAlmostEqual(volumes.get((base_plate, original_wells[source["Replace Well"]]), 0), 500 * (1-fraction))

    def test_per_site_core_api_validates_overrides(self):
        path = SHEETS / "PAINT_R1_replace.csv"
        name = "U-Apt_H01-06_R1"
        for overrides in ({"missing": 0}, {name: .33}, {name: float("nan")}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                generate_picklist(SHEETS / "book_base.csv", [ReplacementSelection(path, [name], "R1", site_fractions=overrides)],
                                  all_384_wells(), transfer_volume_nl=100)


class EncodingSchemeTests(unittest.TestCase):
    def test_quadrant_codes_and_alignment_placement_preserve_physical_origami(self):
        from alignment_reconstruction import transformed_sites, AlignmentSettings
        key = "quadrants_2x2"
        items = compile_designs(solid((128,128,128)),100,{c:1 for c in CHANNELS},key)
        self.assertEqual(len(items),48)
        self.assertEqual(len({d["barcode_code"] for d in items}),48)
        self.assertEqual(len(geometry(key)["reserved_codes"]),16)
        for d in items:
            self.assertEqual(d["bits"][2:], [d["quadrant_y"],d["quadrant_x"],d["local_x_shift"],d["local_y_shift"]])
            for site in d["image_sites"]:
                self.assertEqual(site["pixel_x"]//12,d["quadrant_x"])
                self.assertEqual(site["pixel_y"]//16,d["quadrant_y"])
                self.assertAlmostEqual(site["x_nm"]-d["x_shift_nm"],(6-int(site["column"]))*9.9)
                self.assertAlmostEqual(site["y_nm"]-d["y_shift_nm"],((int(site["row"][1:])-1)//2)*5.1)
        first = items[0]
        opposite = next(d for d in items if d["channel"]==0 and d["quadrant"]==3 and d["local_x_shift"]==d["local_y_shift"]==0)
        a = transformed_sites(first,AlignmentSettings(),[1,2,8])
        b = transformed_sites(opposite,AlignmentSettings(),[1,2,8])
        np.testing.assert_allclose(b-a,np.tile([59.4,40.8],(48,1)),atol=1e-12)
        self.assertEqual(len(prepare(items)),48)

    def test_joint_quadrants_redistribute_codes_and_preserve_site_positions(self):
        from alignment_reconstruction import transformed_sites, AlignmentSettings
        for key,expected in (("quadrants_joint_6x3",[6,6,3,3]),("quadrants_joint_7x3",[8,6,4,3]),("quadrants_joint_5x4",[6,4,6,4])):
            scheme = ENCODING_SCHEMES[key]
            pixels = [[[128]*3 for _ in range(6*scheme.x_states)] for _ in range(8*scheme.y_states)]
            items = compile_designs(pixels,100,{c:1 for c in CHANNELS},key)
            info = geometry(key)
            self.assertEqual(len(items),3*sum(expected))
            self.assertLessEqual(len(items),64)
            self.assertEqual([sum(d["channel"]==0 and d["quadrant"]==q for d in items) for q in range(4)],expected)
            self.assertEqual(info["parent_scheme"],scheme.parent_scheme)
            self.assertTrue(info["nonuniform_sampling"])
            self.assertAlmostEqual(info["pixel_edges_x_nm"][-1],118.8)
            self.assertAlmostEqual(info["pixel_edges_y_nm"][-1],81.6)
            for q in range(4):
                group = [d for d in items if d["channel"]==0 and d["quadrant"]==q]
                self.assertAlmostEqual(sum(d["quadrant_normalization_weight"] for d in group),sum(expected)/4)
                design = group[0]
                physical = transformed_sites(design,AlignmentSettings(),[1,2,3])
                origin = dict(design,x_shift_nm=0,y_shift_nm=0,image_sites=[dict(s,x_nm=s["x_nm"]-design["x_shift_nm"],y_nm=s["y_nm"]-design["y_shift_nm"]) for s in design["image_sites"]])
                unplaced = transformed_sites(origin,AlignmentSettings(),[1,2,3])
                np.testing.assert_allclose(physical-unplaced,np.tile([design["x_shift_nm"],design["y_shift_nm"]],(48,1)),atol=1e-12)

    def test_nine_bit_layout_parity_and_real_source_wells(self):
        from image_encoding import NINE_BIT_SITES
        self.assertEqual(NINE_BIT_SITES[0], (("H01","12"),("H03","12")))
        self.assertEqual(NINE_BIT_SITES[4], (("H07","09"),("H09","09")))
        self.assertEqual(NINE_BIT_SITES[8], (("H13","08"),("H15","08")))
        for key in ("nine_fixed_16x8", "nine_joint_18x9", "nine_parity_8x8"):
            scheme = ENCODING_SCHEMES[key]
            pixels = [[[100,150,200] for _ in range(6*scheme.x_states)] for _ in range(8*scheme.y_states)]
            items = compile_designs(pixels,50,{c:5 for c in CHANNELS},key)
            self.assertEqual(len(items),scheme.design_count)
            codes = {d["barcode_code"] for d in items}
            self.assertEqual(len(codes),len(items))
            self.assertEqual(len(geometry(key)["barcode_bits"]),9)
            for d in items:
                if scheme.parity:
                    self.assertEqual(sum(d["bits"])%2,0)
                    for bit in range(9):
                        self.assertNotIn(d["barcode_code"]^(1<<bit),codes)
                for bit,sites in zip(d["bits"],NINE_BIT_SITES):
                    for row,col in sites:
                        self.assertEqual(d["site_fractions"][site_name(row,col)],bit)
            # Verify that the new corner/offset barcode staples resolve in real source sheets.
            self.assertEqual(len(prepare(items[:1])),1)

    def test_all_schemes_cover_every_pixel_once_and_use_distinct_barcode_codes(self):
        for scheme_id, scheme in ENCODING_SCHEMES.items():
            with self.subTest(scheme=scheme_id):
                width, height = 6 * scheme.x_states, 8 * scheme.y_states
                pixels = [[[(x*5)%256, (y*7)%256, (x+y)%256] for x in range(width)] for y in range(height)]
                items = compile_designs(pixels, 25, {color: 255 for color in CHANNELS}, scheme_id)
                self.assertEqual(len(items), scheme.design_count)
                self.assertEqual(reconstruct(items), pixels)
                geometry_info = geometry(scheme_id)
                codebook = {entry["code"]: entry for entry in geometry_info["codebook"]}
                self.assertEqual(len(codebook), scheme.design_count)
                self.assertEqual(set(codebook) | set(geometry_info["reserved_codes"]), set(range(1 << scheme.bit_count)))
                seen = set()
                for item in items:
                    self.assertEqual(len(item["bits"]), scheme.bit_count)
                    self.assertLess(item["barcode_code"], 1 << scheme.bit_count)
                    entry = codebook[item["barcode_code"]]
                    self.assertEqual((entry["color"], entry["x_shift"], entry["y_shift"]),
                                     (item["color"], item["x_shift"], item["y_shift"]))
                    if scheme.joint:
                        color_index, offset = divmod(item["barcode_code"], scheme.x_states * scheme.y_states)
                        y_shift, x_shift = divmod(offset, scheme.x_states)
                        self.assertEqual((color_index, x_shift, y_shift), (item["channel"], item["x_shift"], item["y_shift"]))
                    for site in item["image_sites"]:
                        key = (site["pixel_x"], site["pixel_y"], item["channel"])
                        self.assertNotIn(key, seen)
                        seen.add(key)
                        if scheme.tiled:
                            self.assertEqual(site["pixel_x"], item["quadrant_x"]*6*scheme.x_counts[0]+(6-int(site["column"]))*item["local_x_states"]+item["local_x_shift"])
                            self.assertEqual(site["pixel_y"], item["quadrant_y"]*8*scheme.y_counts[0]+((int(site["row"][1:])-1)//2)*item["local_y_states"]+item["local_y_shift"])
                        else:
                            self.assertEqual(site["pixel_x"], (6-int(site["column"])) * scheme.x_states + item["x_shift"])
                            self.assertEqual(site["pixel_y"], ((int(site["row"][1:])-1)//2) * scheme.y_states + item["y_shift"])
                        self.assertAlmostEqual(site["x_nm"], geometry_info["pixel_centers_x_nm"][site["pixel_x"]])
                        self.assertAlmostEqual(site["y_nm"], geometry_info["pixel_centers_y_nm"][site["pixel_y"]])
                    for bit, sites in zip(item["bits"], scheme.bit_sites):
                        for row, column in sites:
                            self.assertEqual(item["site_fractions"][site_name(row, column)], bit)
                self.assertEqual(len(seen), width * height * 3)
                self.assertAlmostEqual(geometry_info["field_width_nm"], 118.8 if scheme.tiled else 59.4)
                self.assertAlmostEqual(geometry_info["field_height_nm"], 81.6 if scheme.tiled else 40.8)

    def test_binning_and_physical_exports_have_matching_geometry_for_every_scheme(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.png"
            Image.new("RGB", (792, 544), (255, 128, 0)).save(path)
            for scheme_id, scheme in ENCODING_SCHEMES.items():
                with self.subTest(scheme=scheme_id):
                    pixels = bin_image(path, scheme_id=scheme_id)
                    self.assertEqual(len(pixels), 8 * scheme.y_states)
                    self.assertTrue(all(len(row) == 6 * scheme.x_states for row in pixels))
                    self.assertTrue(all(pixel == [255,128,0] for row in pixels for pixel in row))
                    raster_width, raster_height = physical_raster_size(scheme_id)
                    # Unequal tile partitions use physical cell edges, not a
                    # uniformly spaced global sample-index grid. Large coprime
                    # partitions also use a bounded raster rather than their LCM.
                    info = geometry(scheme_id)
                    self.assertAlmostEqual(raster_width / raster_height,
                                           info["field_width_nm"] / info["field_height_nm"])
                    self.assertGreaterEqual(raster_width, 6 * scheme.tile_columns * max(scheme.x_counts))
                    self.assertGreaterEqual(raster_height, 8 * scheme.tile_rows * max(scheme.y_counts))

    def test_joint_batches_allocate_all_runs_and_reserved_codes_are_excluded(self):
        for scheme_id, scheme in ENCODING_SCHEMES.items():
            if not scheme.joint:
                continue
            pixels = [[[255,128,0] for _ in range(6*scheme.x_states)] for _ in range(8*scheme.y_states)]
            items = compile_designs(pixels, 50, {color: 5 for color in CHANNELS}, scheme_id)
            if scheme.design_count * 3 > 384:
                with self.assertRaisesRegex(ValueError, "destination well"):
                    prepare(items)
                continue
            runs = prepare(items)
            self.assertEqual(len(runs), scheme.design_count)
            wells = [well for run in runs for well in run["destination_wells"]]
            self.assertEqual(len(wells), len(set(wells)))
            self.assertEqual(len(wells), scheme.design_count * 3)
            self.assertEqual({item["barcode_code"] for item in items}, set(range(scheme.design_count)))

    def test_origami_rotation_keeps_source_upright_for_every_scheme(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "quadrants.png"
            image = Image.new("RGB", (544,792), "red")
            image.paste((0,255,0), (272,0,544,396))
            image.paste((0,0,255), (0,396,272,792))
            image.paste((255,255,0), (272,396,544,792))
            image.save(path)
            for scheme_id in ENCODING_SCHEMES:
                views = {}
                for angle in (0,90,180,270):
                    with self.subTest(scheme=scheme_id, angle=angle):
                        info = geometry(scheme_id, angle)
                        local = bin_image(path, rotation_degrees=angle, scheme_id=scheme_id)
                        upright = rotate_pixels(local, angle)
                        views[angle] = upright
                        self.assertEqual((len(upright[0]), len(upright)), (info["display_columns"], info["display_rows"]))
                        physical = physical_reference_raster(Image.fromarray(np.asarray(local,dtype=np.uint8)),scheme_id,angle)
                        for fx,fy,color in ((.4,.25,[255,0,0]),(.6,.25,[0,255,0]),(.4,.75,[0,0,255]),(.6,.75,[255,255,0])):
                            self.assertEqual(list(physical.getpixel((int(physical.width*fx),int(physical.height*fy)))),color)
                        items = compile_designs(local, 25, {color:255 for color in CHANNELS}, scheme_id, angle)
                        self.assertEqual(rotate_pixels(reconstruct(items), angle), upright)
                        for item in items:
                            for site in item["image_sites"]:
                                x, y = site["display_pixel_x"], site["display_pixel_y"]
                                self.assertEqual(site["rgb"], upright[y][x])
                                expected = rotate_point(site["x_nm"],site["y_nm"],info["center_span_x_nm"],info["center_span_y_nm"],angle)
                                self.assertAlmostEqual(site["display_x_nm"],expected[0])
                                self.assertAlmostEqual(site["display_y_nm"],expected[1])
                if not info["nonuniform_sampling"]:
                    self.assertEqual(views[0], views[180])
                    self.assertEqual(views[90], views[270])

    def test_invalid_scheme_and_mixed_reconstruction_fail(self):
        with self.assertRaisesRegex(ValueError, "Unknown"):
            geometry("unknown")
        with self.assertRaises(ValueError):
            barcode_bits("R1 R", 6, 0, "joint_6x3")
        with self.assertRaises(ValueError):
            compile_designs(solid((0,0,0)), 50, {color: 5 for color in CHANNELS}, "joint_7x3")
        first = designs()[0]
        second = dict(first, encoding_scheme="joint_7x3")
        with self.assertRaisesRegex(ValueError, "different encoding"):
            reconstruct([first, second])


class ImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import picklist_app
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for name in ("CONFIG_PATH", "PLATE_STATE_PATH", "STORAGE_STATE_PATH"):
            patcher = patch.object(picklist_app, name, self.folder / (name + ".json"))
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.app = picklist_app.PicklistApp()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self.view = self.app.rgb_image_view
        self.image_path = self.folder / "sample.png"
        Image.new("RGB", (792, 544), (255, 128, 0)).save(self.image_path)

    def test_load_prepare_export_store_and_invalidate(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            self.assertEqual(len(view.designs), 48)
            view._prepare()
            error.assert_not_called()
            self.assertEqual(len(view.runs), 48)
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            error.assert_not_called()
            self.assertTrue(view.saved)
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output / "image_manifest.json").read_text())
            self.assertEqual(len(manifest["designs"]), 48)
            self.assertEqual(len(manifest["runs"]), 48)
            self.assertEqual(len(list(output.glob("*/picklist.csv"))), 48)
            self.assertEqual(len(list(output.glob("*/mixing_recipe.csv"))), 48)
            self.assertEqual(len((output / "pixel_assignments.csv").read_text().splitlines()), 2305)
            before = json.loads(json.dumps(self.app.plate_state))
            view._store()
            view._store()
            error.assert_not_called()
            self.assertEqual(len(self.app._storage_items()), 48)
            self.assertEqual(self.app.plate_state, before)
            stored = load_json(view.storage_path, {})
            self.assertEqual(stored["items"][0]["image_design"]["image_sites"][0]["rgb"], [255,128,0])
            self.view.final_volume.set("400")
            self.assertEqual(view.runs, [])
            self.assertEqual(str(view.export_button["state"]), "disabled")
            self.assertEqual(len(view.designs), 48)

    def test_linear_light_defaults_toggle_invalidation_and_export_metadata(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            self.assertTrue(view.linear_light.get())
            self.assertTrue(view.normalize_designs.get())
            view.image_path.set(str(self.image_path))
            view._load()
            green = next(d for d in view.designs if d["channel"]==1)
            self.assertAlmostEqual(green["image_sites"][0]["replacement_fraction"],.2)
            view._prepare()
            view.linear_light.set(False)
            view._light_changed()
            self.assertEqual(view.runs,[])
            green = next(d for d in view.designs if d["channel"]==1)
            self.assertAlmostEqual(green["image_sites"][0]["replacement_fraction"],.5)
            view.linear_light.set(True)
            view._light_changed()
            view._prepare()
            with patch("rgb_image_view.filedialog.askdirectory",return_value=str(self.folder)):
                view._export()
            error.assert_not_called()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output/"image_manifest.json").read_text())
            self.assertEqual(manifest["color_processing"],{"linear_light":True,"normalize_designs":True,"compensate_area":True})
            self.assertEqual(manifest["gaussian_reconstruction"]["display_encoding"],"sRGB")
            with Image.open(output/"quantized_rgb.png") as image:
                self.assertEqual(image.getpixel((0,0)),(255,124,0))
            view.scheme.set(ENCODING_SCHEMES["nine_joint_18x9"].label)
            expected = view.reconstruction
            self.assertAlmostEqual(expected.metadata["design_abundance_scale"],48/486)
            view.normalize_designs.set(False)
            view._light_changed()
            np.testing.assert_array_equal(expected.density,view.reconstruction.density)
            self.assertGreater(view.reconstruction.metadata["clipped_pixel_fraction"],expected.metadata["clipped_pixel_fraction"])

    def test_quadrant_preview_optimizer_and_export_use_full_composite(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view.scheme.set(ENCODING_SCHEMES["quadrants_2x2"].label)
            view.gaussian_exposure.set("1")
            view.optimize_enabled.set(True)
            view._volumes_changed()
            self.assertEqual(len(view.designs),48)
            self.assertAlmostEqual(view.image_geometry["field_width_nm"],118.8)
            self.assertAlmostEqual(view.image_geometry["field_height_nm"],81.6)
            self.assertLessEqual(view.optimization_metrics["optimized_rmse"],view.optimization_metrics["baseline_rmse"])
            view.design_name.set(view.designs[-1]["name"])
            view._draw_design()
            self.assertEqual(len(view.pixel_canvases[0].find_withtag("design_pixels")),96)
            self.assertIn("bottom-right",view.design_summary.get())
            view._prepare()
            self.assertEqual(len(view.runs),48)
            with patch("rgb_image_view.filedialog.askdirectory",return_value=str(self.folder)):
                view._export()
            error.assert_not_called()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output/"image_manifest.json").read_text())
            self.assertEqual(manifest["geometry"]["quadrant_count"],4)
            self.assertEqual(manifest["gaussian_reconstruction"]["canvas_area_gain"],4)
            self.assertTrue(manifest["color_processing"]["compensate_area"])
            self.assertEqual({d["quadrant"] for d in manifest["designs"]},{0,1,2,3})
            self.assertIn("quadrant_offset_x_nm",(output/"pixel_assignments.csv").read_text().splitlines()[0])
            with np.load(output/"gaussian_reconstruction_density.npz") as data:
                np.testing.assert_array_equal(data["density"],view.reconstruction.density)

    def test_each_six_bit_family_has_a_quadrant_card_below_it_and_exports(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view._compare_schemes()
            comparison = view.comparison_window
            comparison.withdraw()
            for key,scheme in ENCODING_SCHEMES.items():
                if not scheme.tiled:
                    continue
                parent_card = comparison.previews[scheme.parent_scheme][0].master.grid_info()
                tile_card = comparison.previews[key][0].master.grid_info()
                self.assertEqual(parent_card["column"],tile_card["column"])
                self.assertEqual(parent_card["row"],0)
                self.assertEqual(tile_card["row"],1)
                self.assertLessEqual(scheme.design_count,64)
            comparison.use_scheme("quadrants_joint_6x3")
            self.assertEqual(len(view.designs),54)
            upper = view._pixel_bounds(0,0)
            lower = view._pixel_bounds(0,16)
            self.assertAlmostEqual(lower[3]-lower[1],2*(upper[3]-upper[1]))
            view._prepare()
            self.assertEqual(len(view.runs),54)
            with patch("rgb_image_view.filedialog.askdirectory",return_value=str(self.folder)):
                view._export()
            error.assert_not_called()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output/"image_manifest.json").read_text())
            self.assertTrue(manifest["geometry"]["nonuniform_sampling"])
            self.assertEqual(manifest["geometry"]["parent_scheme"],"joint_6x3")
            self.assertEqual(len(manifest["runs"]),54)
            self.assertIn("quadrant_normalization_weight",(output/"pixel_assignments.csv").read_text().splitlines()[0])

    def test_nine_bit_schemes_are_selectable_from_comparison_and_inspectable(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view._compare_schemes()
            view.comparison_window.withdraw()
            self.assertEqual(len(view.comparison_window.images),len(ENCODING_SCHEMES))
            for key in ("nine_fixed_16x8", "nine_joint_18x9", "nine_parity_8x8"):
                view.comparison_window.use_scheme(key)
                self.assertEqual(view._scheme_id(),key)
                self.assertEqual(len(view.designs),ENCODING_SCHEMES[key].design_count)
                self.assertEqual(len(view.designs[0]["bits"]),9)
                self.assertIn("9 bits",view.comparison_window.metrics[key].get())
                self.assertEqual(len(view.pixel_canvases[0].find_withtag("design_pixels")),96)
            self.assertIn("parity",view.explanation.get())
            error.assert_not_called()

    def test_scheme_switch_rebins_updates_outlines_and_exports_codebook(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view._prepare()
            for scheme_id in ("joint_6x3", "joint_5x4", "joint_7x3"):
                scheme = ENCODING_SCHEMES[scheme_id]
                view.scheme.set(scheme.label)
                self.assertEqual(view.runs, [])
                self.assertEqual(str(view.export_button["state"]), "disabled")
                self.assertEqual(len(view.designs), scheme.design_count)
                self.assertEqual(len(view.pixels), scheme.y_states*8)
                self.assertEqual(len(view.pixels[0]), scheme.x_states*6)
                self.assertIn(str(scheme.design_count), view.prepare_button["text"])
                self.assertEqual(self.app.config["image_encoding_scheme"], scheme_id)
                view.design_name.set(view.designs[-1]["name"])
                view._draw_design()
                canvas = view.pixel_canvases[0]
                outlines = canvas.find_withtag("design_pixels")
                self.assertEqual(len(outlines), 96)
                for item, site in zip(outlines[1::2], view.designs[-1]["image_sites"]):
                    self.assertEqual(canvas.coords(item), list(view._pixel_bounds(site["pixel_x"], site["pixel_y"])))
                self.assertEqual(len(canvas.find_all()), scheme.x_states*6 * scheme.y_states*8 + 96)
                view._prepare()
                self.assertEqual(len(view.runs), scheme.design_count)
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            view._store()
            error.assert_not_called()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output / "image_manifest.json").read_text())
            self.assertEqual(manifest["encoding_scheme"], "joint_7x3")
            self.assertEqual(len(manifest["geometry"]["codebook"]), 63)
            self.assertEqual(manifest["geometry"]["reserved_codes"], [63])
            self.assertEqual(len(list(output.glob("*/picklist.csv"))), 63)
            with Image.open(output / "binned_rgb.png") as raster:
                self.assertEqual(raster.size, (42, 24))
            with Image.open(output / "binned_rgb_physical.png") as raster:
                self.assertEqual(raster.size, physical_raster_size("joint_7x3"))
            stored = load_json(view.storage_path, {})["items"]
            self.assertEqual(len(stored), 63)
            self.assertTrue(all(item["image_encoding_scheme"] == "joint_7x3" for item in stored))
            self.assertEqual(stored[0]["image_geometry"]["reserved_codes"], [63])
            self.app._flush_config()
            self.assertEqual(load_json(self.folder / "CONFIG_PATH.json", {})["image_encoding_scheme"], "joint_7x3")
            view.scheme.set(ENCODING_SCHEMES[DEFAULT_SCHEME].label)
            self.assertEqual(len(view.designs), 48)
            self.assertEqual(view.designs[0]["bits"], [1,1,0,0,0,0])

    def test_comparison_matches_each_scheme_and_updates_without_preparing_runs(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view._prepare()
            prepared = view.runs
            selected = view.scheme.get()
            view._compare_schemes()
            comparison = view.comparison_window
            comparison.withdraw()
            self.assertIs(view.runs, prepared)
            self.assertEqual(view.scheme.get(), selected)
            self.assertEqual(set(comparison.images), set(ENCODING_SCHEMES))
            for scheme_id, (binned, achievable) in comparison.images.items():
                info = geometry(scheme_id)
                self.assertEqual((len(binned[0]), len(binned)), (info["columns"], info["rows"]))
                expected = render_gaussian_overlay(compile_designs(binned, 50, {color: 5 for color in CHANNELS}, scheme_id, linear_light=True), linear_light=True, normalize_designs=True, compensate_area=True)
                np.testing.assert_allclose(achievable.density, expected.density)
                overlay_canvas = comparison.previews[scheme_id][1]
                self.assertEqual(len(overlay_canvas.find_all()), 1)
                self.assertEqual(overlay_canvas.type(overlay_canvas.find_all()[0]), "image")
                for canvas in comparison.previews[scheme_id][:1]:
                    self.assertEqual(len(canvas.find_all()), info["columns"] * info["rows"])
                    last = canvas.coords(canvas.find_all()[-1])
                    self.assertAlmostEqual(last[2], 258)
                    self.assertAlmostEqual(last[3], 7 + 252 * 40.8 / 59.4)
            self.assertEqual(comparison.images[DEFAULT_SCHEME][0], view.pixels)
            self.view.final_volume.set("100")
            expected = render_gaussian_overlay(compile_designs(view.pixels, 25, {color:5 for color in CHANNELS}, linear_light=True), linear_light=True, normalize_designs=True, compensate_area=True)
            np.testing.assert_allclose(comparison.images[DEFAULT_SCHEME][1].density, expected.density)
            comparison.use_scheme("joint_7x3")
            self.assertEqual(view._scheme_id(), "joint_7x3")
            self.assertEqual(view.runs, [])
            self.assertEqual(comparison.buttons["joint_7x3"]["text"], "Selected")
            view._rotate(90)
            self.assertEqual(comparison.images["joint_7x3"][0], view.pixels)
            self.assertIn("90°", comparison.summary.get())
            self.view.final_volume.set("")
            self.assertEqual(comparison.images, {})
            self.assertTrue(all(not canvas.find_all() for canvases in comparison.previews.values() for canvas in canvases))
            self.view.final_volume.set("200")
            self.assertEqual(len(comparison.images), len(ENCODING_SCHEMES))
            comparison.destroy()
            view._compare_schemes()
            self.assertIsNot(view.comparison_window, comparison)
            self.assertEqual(len(view.comparison_window.images), len(ENCODING_SCHEMES))
            error.assert_not_called()

    def test_comparison_uses_custom_rgb_settings_for_every_scheme(self):
        view = self.view
        # An asymmetric, nonuniform image exposes fit/rotation and light-space errors.
        image = Image.new("RGB", (120, 80), (43, 91, 173))
        image.paste((209, 67, 12), (0, 0, 40, 50))
        image.save(self.image_path)
        view.fit_mode.set("Center crop")
        view.rotation.set("90")
        view.gaussian_fwhm.set("9")
        view.gaussian_exposure.set("0.7")
        self.view.final_volume.set("300")
        multipliers = dict(zip(CHANNELS, (2, 3, 4)))
        for color, multiplier in multipliers.items():
            view.color_views[color].desired_concentration_nm.set(str(50 * multiplier))
        view.image_path.set(str(self.image_path))
        view._load()
        view._compare_schemes()
        comparison = view.comparison_window
        comparison.withdraw()
        for enabled in (False, True):
            view.linear_light.set(enabled)
            view.normalize_designs.set(enabled)
            view.compensate_area.set(enabled)
            view._light_changed()
            self.assertEqual(set(comparison.images), set(ENCODING_SCHEMES))
            for scheme_id, (pixels, result) in comparison.images.items():
                expected_pixels = bin_image(self.image_path, "Center crop", 90, scheme_id, linear_light=enabled)
                self.assertEqual(pixels, expected_pixels)
                designs = compile_designs(expected_pixels, 75, multipliers, scheme_id, 90, linear_light=enabled)
                expected = render_gaussian_overlay(designs, 9, .7, linear_light=enabled,
                                                  normalize_designs=enabled, compensate_area=enabled)
                np.testing.assert_array_equal(result.rgb, expected.rgb)
                np.testing.assert_allclose(result.density, expected.density)
            np.testing.assert_array_equal(comparison.images[view._scheme_id()][1].rgb, view.reconstruction.rgb)
            self.assertIn("optimization OFF", comparison.summary.get())
            self.assertIn("Base transfer 75 nL", comparison.summary.get())
            self.assertEqual(comparison.optimization_metrics, {})

    def test_rotated_origami_previews_and_exports_keep_the_image_upright(self):
        view = self.view
        image = Image.new("RGB", (544,792), "red")
        image.paste("blue", (0,396,544,792))
        image.save(self.image_path)
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view._rotate(90)
            self.assertEqual((view.image_geometry["display_columns"], view.image_geometry["display_rows"]), (32,24))
            self.assertEqual((view.image_geometry["display_field_width_nm"], view.image_geometry["display_field_height_nm"]), (40.8,6*9.9))
            canvas = view.pixel_canvases[0]
            # Source remains red on top and blue on bottom despite a portrait origami field.
            rectangles = canvas.find_all()[:24*32]
            for index, rectangle in enumerate(rectangles):
                local_y, local_x = divmod(index, 24)
                display_x, display_y = rotate_point(local_x, local_y, 23,31,90)
                if display_x == 16 and display_y in (2,21):
                    self.assertEqual(canvas.itemcget(rectangle,"fill"), "#ff0000" if display_y == 2 else "#0000ff")
            self.assertEqual(int(view.design_canvas["height"]), 570)
            labels = [view.design_canvas.coords(item) for item in view.design_canvas.find_all()
                      if view.design_canvas.type(item) == "text"]
            self.assertGreater(labels[-1][1], labels[0][1])
            outlines = canvas.find_withtag("design_pixels")
            for item, site in zip(outlines[1::2], view.designs[0]["image_sites"]):
                self.assertEqual(canvas.coords(item), list(view._pixel_bounds(site["pixel_x"],site["pixel_y"])))
            view._prepare()
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            view._store()
            error.assert_not_called()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output / "image_manifest.json").read_text())
            self.assertEqual(manifest["origami_rotation_degrees_clockwise"],90)
            self.assertEqual(manifest["source_image_rotation_degrees"],0)
            with Image.open(output / "binned_rgb.png") as raster:
                self.assertEqual(raster.size,(32,24))
                self.assertEqual(raster.getpixel((16,2)),(255,0,0))
                self.assertEqual(raster.getpixel((16,21)),(0,0,255))
            with Image.open(output / "binned_rgb_physical.png") as raster:
                self.assertEqual(raster.size,(544,792))
            stored = load_json(view.storage_path,{})["items"]
            self.assertTrue(all(item["origami_rotation_degrees_clockwise"] == 90 for item in stored))
            self.assertTrue(all(item["image_geometry"]["display_columns"] == 32 for item in stored))

    def test_optimized_assignments_compare_export_and_reset(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            baseline = json.loads(json.dumps(view.designs))
            view.optimize_enabled.set(True)
            view._volumes_changed()
            self.assertIsNotNone(view.optimization_metrics)
            self.assertLess(view.optimization_metrics["optimized_rmse"],view.optimization_metrics["baseline_rmse"])
            self.assertNotEqual(view.designs,baseline)
            view._compare_schemes()
            view.comparison_window.withdraw()
            self.assertTrue(all("Optimized RMSE" in metric.get() for metric in view.comparison_window.metrics.values()))
            self.assertEqual(set(view.comparison_window.optimization_metrics), set(ENCODING_SCHEMES))
            self.assertIn("optimization ON", view.comparison_window.summary.get())
            np.testing.assert_array_equal(view.comparison_window.images[view._scheme_id()][1].rgb, view.reconstruction.rgb)
            view._prepare()
            self.assertEqual(view.snapshot["designs"],view.designs)
            self.assertEqual(view.snapshot["optimization"],view.optimization_metrics)
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output / "image_manifest.json").read_text())
            self.assertEqual(manifest["designs"],view.designs)
            self.assertTrue(manifest["optimization"]["barcode_bits_fixed"])
            view.optimize_enabled.set(False)
            view._volumes_changed()
            self.assertEqual(view.designs,baseline)
            self.assertEqual(view.comparison_window.optimization_metrics, {})
            self.assertIn("optimization OFF", view.comparison_window.summary.get())
            self.assertIsNone(view.optimization_metrics)
            self.assertEqual(view.runs,[])
            error.assert_not_called()

    def test_alignment_workflow_optimization_comparison_export_and_invalidation(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view.rotation.set("90")
            view._load()
            view.alignment_values["origami_per_design"].set("3")
            view.alignment_enabled.set(True)
            view.optimize_enabled.set(True)
            view._volumes_changed()
            self.assertEqual(len(view.designs),48)
            self.assertIsNotNone(view.alignment_example)
            metrics = view.optimization_metrics
            self.assertNotEqual(metrics["training_seed"],metrics["validation_seed"])
            self.assertFalse(metrics["validation_used_for_selection"])
            self.assertLessEqual(metrics["training_optimized"]["objective_mse"],metrics["training_baseline"]["objective_mse"])
            original = view.alignment_example.density.copy()
            expected = view.reconstruction.density.copy()
            view._new_alignment_example()
            self.assertFalse(np.array_equal(original,view.alignment_example.density))
            np.testing.assert_array_equal(expected,view.reconstruction.density)
            view._compare_schemes()
            view.comparison_window.withdraw()
            self.assertEqual(len(view.comparison_window.images),len(ENCODING_SCHEMES))
            self.assertTrue(all("Held-out RMSE" in metric.get() for metric in view.comparison_window.metrics.values()))
            np.testing.assert_array_equal(view.comparison_window.images[view._scheme_id()][1].rgb, view.reconstruction.rgb)
            for _, result in view.comparison_window.images.values():
                self.assertEqual(result.metadata["alignment"], vars(view._alignment_settings()))
            self.assertIn("3 origami/design", view.comparison_window.summary.get())
            view._prepare()
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            view._store()
            error.assert_not_called()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output / "image_manifest.json").read_text())
            self.assertEqual(manifest["alignment_simulation"]["alignment"]["origami_per_design"],3)
            self.assertEqual(manifest["alignment_example_seed"],view.example_seed)
            self.assertEqual(manifest["optimization"]["validation_optimized"],metrics["validation_optimized"])
            with np.load(output / "gaussian_reconstruction_density.npz") as data:
                np.testing.assert_array_equal(data["density"],view.reconstruction.density)
            with Image.open(output / "alignment_example.png") as image:
                np.testing.assert_array_equal(np.asarray(image),np.asarray(view.alignment_example.rgb))
            self.assertTrue((output / "ideal_alignment.png").exists())
            self.assertTrue(all(item["alignment_simulation"]["alignment"]["origami_per_design"]==3
                                for item in load_json(view.storage_path,{})["items"]))
            view.alignment_values["rotation_deg"].set("-1")
            self.assertIsNone(view.reconstruction)
            self.assertEqual(view.runs,[])
            self.assertEqual(view.comparison_window.images,{})
            view.alignment_enabled.set(False)
            view._volumes_changed()
            self.assertEqual(len(view.designs),48)
            self.assertIsNone(view.alignment_example)
            self.assertNotIn("alignment",view.reconstruction.metadata)

    def test_gaussian_width_and_exposure_update_previews_and_exports(self):
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            self.assertEqual(float(view.gaussian_fwhm.get()),7.5)
            view._compare_schemes()
            view.comparison_window.withdraw()
            original = view.reconstruction.density.copy()
            view.gaussian_fwhm.set("4")
            self.assertEqual(view.reconstruction.metadata["fwhm_nm"],4)
            self.assertFalse(np.array_equal(original,view.reconstruction.density))
            self.assertTrue(all(result.metadata["fwhm_nm"] == 4 for _,result in view.comparison_window.images.values()))
            signal = view.reconstruction.density.copy()
            view.gaussian_exposure.set("0.5")
            np.testing.assert_array_equal(signal,view.reconstruction.density)
            self.assertTrue(all(result.metadata["exposure"] == .5 for _,result in view.comparison_window.images.values()))
            view._prepare()
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            view._store()
            error.assert_not_called()
            output = next(self.folder.glob("*/image_manifest.json")).parent
            manifest = json.loads((output / "image_manifest.json").read_text())
            self.assertEqual(manifest["gaussian_reconstruction"]["fwhm_nm"],4)
            with Image.open(output / "gaussian_reconstruction.png") as image:
                np.testing.assert_array_equal(np.asarray(image),np.asarray(view.reconstruction.rgb))
            with np.load(output / "gaussian_reconstruction_density.npz") as archive:
                np.testing.assert_allclose(archive["density"],signal)
            stored = load_json(view.storage_path,{})["items"]
            self.assertTrue(all(item["gaussian_reconstruction"]["exposure"] == .5 for item in stored))
            view.gaussian_fwhm.set("0")
            self.assertIsNone(view.reconstruction)
            self.assertEqual(view.runs,[])
            self.assertEqual(view.comparison_window.images,{})

    def test_storage_retry_does_not_duplicate_designs_after_plate_write_failure(self):
        import rgb_image_view
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view._prepare()
            save = rgb_image_view.save_json
            def fail_plate(path, value):
                if path == view.plate_path:
                    raise OSError("Test write failure")
                save(path, value)
            with patch("rgb_image_view.save_json", side_effect=fail_plate):
                view._store()
            error.assert_called_once()
            self.assertFalse(view.stored)
            self.assertEqual(len(self.app._storage_items()), 48)
            view._store()
            self.assertTrue(view.stored)
            self.assertEqual(len(self.app._storage_items()), 48)
            with patch("rgb_image_view.filedialog.askdirectory", return_value=str(self.folder)):
                view._export()
            self.assertTrue(view.saved)
            error.assert_called_once()

    def test_inspector_shows_selected_prepared_picklist_and_clears_stale_transfers(self):
        from picklist_core import PICKLIST_COLUMNS
        view = self.view
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            self.assertEqual(view.picklist_tree.get_children(), ())
            view._prepare()
            error.assert_not_called()
            for run in (view.runs[0], view.runs[-1]):
                view.design_name.set(run["run_name"])
                view._draw_design()
                displayed = [list(map(str, view.picklist_tree.item(item, "values")))
                             for item in view.picklist_tree.get_children()]
                expected = [[str(row[column]) for column in view.picklist_columns] for row in run["picklist"]]
                self.assertEqual(displayed, expected)
                self.assertIn(run["destination_wells"][0], view.picklist_summary.get())
                self.assertTrue(view.site_tree.get_children())
            view.start_well.set("B01")
            self.assertEqual(view.picklist_tree.get_children(), ())
            self.assertIn("Prepare runs", view.picklist_summary.get())

    def test_preparation_skips_existing_wells_and_detects_later_collisions(self):
        view = self.view
        plate = view.destination_plate.get()
        self.app.plate_state = {"plates": {plate: {"wells": {"A01": {"volume_nL": 25}}}}}
        with patch("rgb_image_view.messagebox.showerror") as error:
            view.image_path.set(str(self.image_path))
            view._load()
            view._prepare()
            error.assert_not_called()
            self.assertEqual(view.runs[0]["destination_wells"][0], "A02")
            self.app.plate_state["plates"][plate]["wells"]["A02"] = {"volume_nL": 25}
            view._store()
            error.assert_called_once()
            self.assertEqual(self.app._storage_items(), [])


if __name__ == "__main__":
    unittest.main()
