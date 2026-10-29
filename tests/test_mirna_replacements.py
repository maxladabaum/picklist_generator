from pathlib import Path
from itertools import product

import pytest

from mirna_replacements import TARGETS, target_designs, anchor_position, anchor_label, replacement_positions
from picklist_core import ReplacementSelection, generate_picklist, parse_source1, parse_source2


SHEETS = Path(__file__).resolve().parents[1] / "replacement_sheets"


def test_anchor_labels_use_name_column_instead_of_plate_diagram():
    records, _ = parse_source2(SHEETS / "miRNA_replace.csv")
    by_well = {row["Well"]: row for row in records}
    for plain, dual, helix in (("C8", "C14", 7), ("C10", "C16", 9), ("C12", "C18", 11)):
        for well in (plain, dual):
            assert anchor_position(by_well[well]["Name"]) == ("p2", f"H{helix:02d}", "03")
            assert anchor_label(by_well[well]["Name"]) == f"p2.H{helix}-3"
    assert anchor_label(by_well["C2"]["Name"]) == "p1.H7-2"


def test_imported_plate_preserves_wells_and_excludes_toehold_locks():
    records, _ = parse_source2(SHEETS / "miRNA_replace.csv")
    assert len(records) == 48
    assert len({row["Well"] for row in records}) == 48
    assert not any("_TL" in row["Name"] or "Toehold" in row["Name"] for row in records)
    by_well = {row["Well"]: row for row in records}
    for plain, dual, target in (("C8", "C14", "P13"), ("C10", "C16", "A14"), ("C12", "C18", "D14")):
        assert by_well[plain]["Replace Well"] == by_well[dual]["Replace Well"] == target
        assert by_well[dual]["Sequence"].startswith(by_well[plain]["Sequence"])
        assert len(by_well[dual]["Sequence"]) > len(by_well[plain]["Sequence"])


def test_all_anchor_positions_match_comparable_r1_replacements():
    anchors, _ = parse_source2(SHEETS / "miRNA_replace.csv")
    r1, _ = parse_source2(SHEETS / "PAINT_R1_replace.csv")
    r1_by_well = {row["Replace Well"]: row["Name"] for row in r1}
    positions = replacement_positions(SHEETS)
    assert len(positions) == 48
    for anchor in anchors:
        helix, column = positions[anchor["Name"]]
        assert r1_by_well[anchor["Replace Well"]] == f"U-Apt_{helix}-{column}_R1"
    by_well = {row["Well"]: positions[row["Name"]] for row in anchors}
    assert [by_well[well] for well in ("C2", "C4", "C6")] == [("H07", "01"), ("H09", "01"), ("H11", "01")]
    assert [by_well[well] for well in ("C8", "C10", "C12")] == [("H05", "12"), ("H07", "12"), ("H09", "12")]
    assert [by_well[well] for well in ("C14", "C16", "C18")] == [by_well[well] for well in ("C8", "C10", "C12")]


def test_unknown_replacement_well_does_not_guess_a_location(tmp_path):
    import csv
    import shutil
    shutil.copy(SHEETS / "PAINT_R1_replace.csv", tmp_path)
    with (SHEETS / "miRNA_replace.csv").open() as handle:
        reader = csv.DictReader(handle)
        fields, records = reader.fieldnames, list(reader)
    records[0]["Replace Well"] = "Z99"
    with (tmp_path / "miRNA_replace.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    with pytest.raises(ValueError, match="No comparable R1 site"):
        replacement_positions(tmp_path)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("mask", list(product((False, True), repeat=3)))
def test_target_panel_generates_only_explicit_choices(target, mask):
    design = target_designs(SHEETS)[target]
    names = [name for name, on in zip(design["target_locks"], mask) if on]
    selections = [ReplacementSelection(SHEETS / "miRNA_replace.csv", names, "SourcePlate10[10]")] if names else []
    picklist = generate_picklist(SHEETS / "book_base.csv", selections, ["A01", "A02", "A03"])
    assert {row["Source Well"] for row in picklist if row["Source Plate Name"] == "SourcePlate10[10]"} == {
        well for well, on in zip(design["target_wells"], mask) if on}
    base, _ = parse_source1(SHEETS / "book_base.csv")
    assert len(picklist) == len(base)


@pytest.mark.parametrize("index", range(3))
def test_plain_and_combined_staples_are_independent_without_substitution(index):
    d = target_designs(SHEETS)["miR-21"]
    for key, expected in (("common_locks", ("C8", "C10", "C12")),
                          ("combined_locks", ("C14", "C16", "C18"))):
        rows = generate_picklist(SHEETS / "book_base.csv",
            [ReplacementSelection(SHEETS / "miRNA_replace.csv", [d[key][index]], "SourcePlate10[10]")],
            ["A01", "A02", "A03"])
        assert [row["Source Well"] for row in rows if row["Source Plate Name"] == "SourcePlate10[10]"] == [expected[index]]
    with pytest.raises(ValueError, match="Duplicate replacement"):
        generate_picklist(SHEETS / "book_base.csv",
            [ReplacementSelection(SHEETS / "miRNA_replace.csv",
                [d["common_locks"][index], d["combined_locks"][index]], "SourcePlate10[10]")],
            ["A01", "A02", "A03"])


@pytest.mark.parametrize("base_file,physical_well", [("book_base.csv", "C21"),
                                                    ("book_base_20uM_interleaved.csv", "E21")])
def test_workbook_c21_maps_to_original_a08_key(base_file, physical_well):
    import csv
    with (SHEETS / "miRNA_replace.csv").open() as handle:
        imported = list(csv.DictReader(handle))
    anchors = [row for row in imported if row["Workbook Replace Well"] == "C21"]
    assert len(anchors) == 10
    assert {row["Replace Well"] for row in anchors} == {"A08"}
    base, plate = parse_source1(SHEETS / base_file, "balanced_rigid")
    assert next(row["Well"] for row in base if row["Replacement Well"] == "A08") == physical_well
    rows = generate_picklist(SHEETS / base_file,
        [ReplacementSelection(SHEETS / "miRNA_replace.csv", [anchors[0]["Name"]], "SourcePlate10[10]")],
        ["A01", "A02", "A03"], hinge_type="balanced_rigid")
    assert len(rows) == len(base)
    assert not any(row["Source Plate Name"] == plate and row["Source Well"] == physical_well for row in rows)


@pytest.fixture
def mirna_view():
    import tkinter as tk
    from unittest.mock import Mock
    import picklist_app
    root = tk.Tk()
    root.withdraw()
    title, filename, plate, panels = next(d for d in picklist_app.panel_definitions() if d[0] == "miRNA")
    view = picklist_app.SmartMiRNASetView(root, title, SHEETS / filename, plate, panels,
                               selection_changed=Mock(), scrollable=False)
    view.replacement_percentage.set("100%")
    yield view
    root.destroy()


def click(view, name, selected=True):
    view.variables[name].set(selected)
    view._on_square_toggled(name)


def test_each_panel_is_separate_and_starts_empty(mirna_view):
    import picklist_app
    view = mirna_view
    title, filename, plate, r1_panels = next(d for d in picklist_app.panel_definitions() if d[0] == "PAINT R1")
    r1_view = picklist_app.SetView(view.panel_host.winfo_toplevel(), title, SHEETS / filename, plate, r1_panels)
    anchors, _ = parse_source2(SHEETS / "miRNA_replace.csv")
    records, _ = parse_source2(SHEETS / "PAINT_R1_replace.csv")
    r1_by_well = {row["Replace Well"]: row["Name"] for row in records}
    matches = {row["Name"]: r1_by_well[row["Replace Well"]] for row in anchors}
    expected = list(TARGETS) + ["Smart common locks (p2)",
                               "p1 — Lock 3′ anchors", "p2 — Direct 5′ anchors"]
    assert [panel["label"] for panel in view.panels] == expected
    assert all(not name.startswith("U-Apt_") for name in view.variables)
    for panel in view.panels:
        view.panel_var.set(panel["label"])
        view._show_panel()
        assert view.selected() == []
        assert not any(variable.get() for variable in view.variables.values())
        assert set(view.circle_sites) == set(panel["sequence_names"].values())
        assert len({id(circle[0]) for circle in view.circle_sites.values()}) == 1
        assert picklist_app.panel_display_columns(panel) == picklist_app.panel_display_columns(r1_panels[0])
        for site, name in panel["sequence_names"].items():
            assert matches[name] == "U-Apt_{}-{}_R1".format(*site)
            assert anchor_position(name)[0] == panel["page"]
            canvas, ring, _, _ = view.circle_sites[name]
            r1_canvas, r1_ring, _, _ = r1_view.circle_sites[matches[name]]
            assert canvas.coords(ring) == pytest.approx(r1_canvas.coords(r1_ring))
            assert picklist_app.panel_template_positions(panel, {name}) == picklist_app.panel_template_positions(r1_panels[0], {matches[name]})
        if panel["label"] in expected[:6]:
            assert len(view.circle_sites) == 3
            canvas = next(iter(view.circle_sites.values()))[0]
            assert canvas.bind("<Button-1>")
            assert sum(canvas.type(item) == "text" and canvas.itemcget(item, "text") == "×" for item in canvas.find_all()) == 93
    view.selection_changed.assert_not_called()


def test_browsing_preserves_only_the_users_selections(mirna_view):
    view = mirna_view
    selected = set()
    for panel in view.panels:
        view.panel_var.set(panel["label"])
        view._show_panel()
        assert set(view.selected()) == selected
        name = next(iter(view.circle_sites))
        click(view, name)
        selected.add(name)
        assert set(view.selected()) == selected
    assert len(selected) == len(view.panels)
    view.clear()
    assert view.selected() == []


def test_manual_panel_choices_generate_without_automatic_additions(tmp_path, monkeypatch):
    from unittest.mock import patch
    import picklist_app
    for name in ("CONFIG_PATH", "PLATE_STATE_PATH", "STORAGE_STATE_PATH"):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + ".json"))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        app.base_plate.set("LowConc base plate — 20 µM (interleaved)")
        app.manual_final_volume.set("100")
        app.manual_base_target.set("5")
        app.manual_scaffold_target.set("1")
        view = next(view for view in app.set_views if view.title == "miRNA")
        view.replacement_percentage.set("100%")
        for label in ("miR-155", "Smart common locks (p2)"):
            view.panel_var.set(label)
            view._show_panel()
            click(view, next(iter(view.circle_sites)))
        assert len(view.selected()) == 2
        r1 = next(v for v in app.set_views if v.title == "PAINT R1")
        r1.replacement_percentage.set("100%")
        click(r1, "U-Apt_H05-12_R1")
        assert r1.selected() == []
        assert app.barcode_guard.fractions()["H05-12"] == 1
        assert app.check_selections()
        with patch("picklist_app.messagebox.showerror") as error:
            app._make_picklist()
            app.generate(app.main_tabs, app.preview_page)
            error.assert_not_called()
        anchors = [row for row in app.last_picklist if row["Source Plate Name"] == "SourcePlate10[10]"]
        assert {row["Source Well"] for row in anchors} == {"E2", "C14"}
        assert not any(row["Source Plate Name"] == "SourcePlate6[6]" for row in app.last_picklist)
        assert any("Panel: miR-155" in row["Sample Comments"] for row in anchors)
        assert any("Panel: Smart common locks (p2)" in row["Sample Comments"] for row in anchors)
        before = list(app.last_picklist)
        view.panel_var.set("miR-210")
        view._show_panel()
        assert app.last_picklist == before
        assert app.main_tabs.tab(app.preview_page, "state") == "normal"
        view.clear()
        assert app.last_picklist == []
        assert app.main_tabs.tab(app.preview_page, "state") == "disabled"
    finally:
        app.destroy()
