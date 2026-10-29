from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from test_rgb_synthesis import app
from picklist_core import parse_source2


def click(view, name, selected=True):
    view.variables[name].set(selected)
    view._on_square_toggled(name)


def setup_views(app, r1_title):
    app.base_plate.set("LowConc base plate — 20 µM (interleaved)")
    app.manual_final_volume.set("100")
    app.manual_base_target.set("5")
    app.manual_scaffold_target.set("1")
    locks = next(view for view in app.set_views if view.title == "miRNA")
    r1 = next(view for view in app.set_views if view.title == r1_title)
    locks.panel_var.set("Smart common locks (p2)")
    locks._show_panel()
    locks.replacement_percentage.set("100%")
    r1.replacement_percentage.set("100%")
    return locks, r1


@pytest.mark.parametrize("r1_title", ["PAINT R1", "LowConc PAINT R1"])
@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("lock_first", [True, False])
def test_overlap_converts_once_and_removes_separate_r1(app, r1_title, index, lock_first):
    locks, r1 = setup_views(app, r1_title)
    plain = list(locks.smart_variants)[index]
    variant = locks.smart_variants[plain]
    records, _ = parse_source2(Path(r1.path_var.get()))
    r1_name = next(row["Name"] for row in records if row["Replace Well"] == variant["replace_well"])
    assert locks.selected() == r1.selected() == []
    if lock_first:
        click(locks, plain)
        assert locks.selected() == [plain]
        click(r1, r1_name)
    else:
        click(r1, r1_name)
        assert locks.selected() == []
        click(locks, plain)
    assert locks.selected() == [variant["name"]]
    assert locks.variables[plain].get()
    assert r1.selected() == []
    assert r1.variables[r1_name].get()
    assert r1.site_fractions[r1_name] == 1
    assert r1._target_label(r1_name) == "via lock"
    assert r1_name not in r1.site_concentrations_nm
    assert variant["combined_well"] in locks.smart_status.get()
    assert app.check_selections()
    assert app.barcode_guard.fractions()[locks.r1_locations[variant["name"]]] == 1
    with patch("picklist_app.messagebox.showerror") as error:
        app._make_picklist()
        app.generate(app.main_tabs, app.preview_page)
        error.assert_not_called()
    replacement = [row for row in app.last_picklist if row["Source Plate Name"] == "SourcePlate10[10]"]
    assert len(replacement) == 1
    assert replacement[0]["Source Well"] == variant["combined_well"]
    assert replacement[0]["Transfer Volume"] == 25  # Common lock 50 nM, stock 200 µM.
    assert "Panel: Smart common locks (p2)" in replacement[0]["Sample Comments"]
    assert variant["name"] in replacement[0]["Sample Comments"]
    assert not any(row["Source Plate Name"] in {"SourcePlate6[6]", "SourcePlate7[7]"} for row in app.last_picklist)
    assert app.last_selection_panels[0]["selected_names"] == [variant["name"]]
    assert app.last_design_names == [variant["name"]]


def test_common_settings_and_other_r1_selections_are_preserved(app):
    locks, r1 = setup_views(app, "LowConc PAINT R1")
    plain = next(iter(locks.smart_variants))
    dual = locks.smart_variants[plain]["name"]
    locks.new_selection_concentration.set("100")
    locks.replacement_percentage.set("50%")
    click(locks, plain)
    other = "U-Apt_H01-01_R1"
    r1.new_selection_concentration.set("25")
    r1.replacement_percentage.set("100%")
    click(r1, other)
    click(r1, "U-Apt_H05-12_R1")
    assert locks.selected() == [dual]
    assert locks.site_target(dual) == 100
    assert locks.site_fractions[dual] == 0.5
    assert r1.selected() == [other]
    assert r1.site_target(other) == 25
    assert app.barcode_guard.fractions()["H05-12"] == 0.5
    with patch("picklist_app.messagebox.showerror") as error:
        app.generate(app.main_tabs, app.preview_page)
        error.assert_not_called()
    combined = next(row for row in app.last_picklist if row["Source Plate Name"] == "SourcePlate10[10]")
    assert combined["Transfer Volume"] == 25
    assert sum(row["Source Plate Name"] == "SourcePlate7[7]" for row in app.last_picklist) == 1
    locks.clear()
    assert "U-Apt_H05-12_R1" in r1.selected()
    assert r1.site_target("U-Apt_H05-12_R1") == 25
    assert r1.site_fractions["U-Apt_H05-12_R1"] == 1


def test_toggling_either_choice_restores_standalone_and_template_position(app):
    locks, r1 = setup_views(app, "PAINT R1")
    plain = next(iter(locks.smart_variants))
    dual = locks.smart_variants[plain]["name"]
    click(locks, plain)
    click(r1, "U-Apt_H05-12_R1")
    locks.template_requested = Mock()
    locks._request_template()
    assert locks.template_requested.call_args.args[2] == [(2, 0)]
    click(locks, plain, False)
    assert locks.selected() == []
    assert dual not in locks.site_fractions
    assert r1.selected() == ["U-Apt_H05-12_R1"]
    click(locks, plain)
    assert locks.selected() == [dual]
    assert r1.selected() == []
    r1.template_requested = Mock()
    r1._request_template()
    assert r1.template_requested.called
    click(r1, "U-Apt_H05-12_R1", False)
    assert locks.selected() == [plain]
    assert not locks.smart_combined
    locks.clear()
    assert locks.selected() == []
    assert not locks.smart_combined


def test_check_handles_both_r1_tabs_without_duplicate_combined_transfer(app):
    locks, r1 = setup_views(app, "PAINT R1")
    low = next(view for view in app.set_views if view.title == "LowConc PAINT R1")
    plain = next(iter(locks.smart_variants))
    # Programmatic changes are also resolved at the compatibility boundary.
    locks.variables[plain].set(True)
    for view in (r1, low):
        view.variables["U-Apt_H05-12_R1"].set(True)
    assert app.check_selections()
    assert locks.selected() == [locks.smart_variants[plain]["name"]]
    assert r1.selected() == low.selected() == []
    for view in (r1, low):
        assert view.variables["U-Apt_H05-12_R1"].get()
    r1.clear()
    assert locks.smart_combined
    low.clear()
    assert locks.selected() == [plain]


def test_other_replacement_conflicts_are_not_automatically_resolved(app):
    locks, _ = setup_views(app, "PAINT R1")
    p1 = next(view for view in app.set_views if view.title == "PAINT P1")
    plain = next(iter(locks.smart_variants))
    records, _ = parse_source2(Path(p1.path_var.get()))
    name = next(row["Name"] for row in records if row["Replace Well"] == locks.smart_variants[plain]["replace_well"])
    p1.replacement_percentage.set("100%")
    click(locks, plain)
    click(p1, name)
    assert not app.check_selections()
    assert locks.selected() == [plain]
    assert p1.selected() == [name]


def test_missing_combined_staple_leaves_conflict_and_selections_intact(app):
    locks, r1 = setup_views(app, "PAINT R1")
    plain = next(iter(locks.smart_variants))
    locks.available.remove(locks.smart_variants[plain]["name"])
    click(locks, plain)
    click(r1, "U-Apt_H05-12_R1")
    assert not app.check_selections()
    assert locks.selected() == [plain]
    assert r1.selected() == ["U-Apt_H05-12_R1"]
