import csv
from pathlib import Path
import pytest
from picklist_core import parse_source1, parse_source2, generate_picklist, ReplacementSelection
SHEETS = Path(__file__).resolve().parents[1] / 'replacement_sheets'
PATH = SHEETS / 'book_base_20uM_interleaved.csv'


def test_mapping_and_logical_keys():
    with PATH.open() as f:
        next(f)
        rows = list(csv.DictReader(f))
    assert len(rows) == 260
    assert len({r['Well Position'] for r in rows}) == 260
    additions = [r for r in rows if r['Usage'] == 'Supplemental']
    assert {r['Well Position'] for r in additions} == {f'P{i}' for i in range(17,25)}
    rows = [r for r in rows if r['Usage'] != 'Supplemental']
    assert len(rows) == 252
    assert len({r['Original Well Position'] for r in rows}) == 252
    assert sum(r['Hinge Type'] == 'balanced_rigid' for r in rows) == 18
    for r in rows:
        plate = int(r['IDT Source Plate'])
        src, dst = r['IDT Source Well'], r['Well Position']
        assert ord(dst[0])-65 == 2*(ord(src[0])-65) + (plate in (3,4))
        assert int(dst[1:]) == int(src[1:]) + (12 if plate in (2,4) else 0)
        assert r['Nominal Concentration µM'] == '20'
    names = {r['Sequence Name']: r for r in rows}
    assert names['H05_ST17[138]15[131]']['Well Position'] == 'G17'
    assert names['H05_ST17[138]15[131]']['Original Well Position'] == 'H05'
    assert names['Bal_Rigid_Hinge_P2_H12_C227']['Well Position'] == 'O22'
    assert names['Bal_Rigid_Hinge_P2_H12_C227']['Original Well Position'] == 'A02'
    with pytest.raises(ValueError, match='hinge type'):
        parse_source1(PATH, 'original')


def test_all_paint_replacements_use_relocated_originals():
    base, plate = parse_source1(PATH, 'balanced_rigid')
    assert plate == 'SourcePlate8[8]'
    physical = {r['Replacement Well']: r['Well'] for r in base}
    replacements, _ = parse_source2(SHEETS / 'PAINT_R1_replace.csv')
    for r in replacements:
        selection = ReplacementSelection(SHEETS / 'PAINT_R1_replace.csv', [r['Name']],
            'SourcePlate7[7]', desired_concentration_nm=25,
            concentration_nm=20000, replacement_fraction=.5)
        picklist = generate_picklist(PATH, [selection], ['A01','A02','A03'],
            transfer_volume_nl=50, base_concentration_nm=20000,
            base_desired_concentration_nm=25, hinge_type='balanced_rigid')
        original = [p for p in picklist if p['Source Plate Name']==plate
                    and p['Source Well']==physical[r['Replace Well'].upper()]]
        assert sum(p['Transfer Volume'] for p in original) == 25
        assert sum(p['Transfer Volume'] for p in picklist
                   if p['Source Plate Name']=='SourcePlate7[7]') == 25


def test_selector_uses_interleaved_plate_and_ordered_hinges(tmp_path, monkeypatch):
    import picklist_app
    from PIL import Image
    from unittest.mock import patch
    for key in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, key, tmp_path / (key + '.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        app.output_root.set(str(tmp_path))
        view = app.rgb_image_view
        view.auto_export_templates.set(False)
        app.base_plate.set('LowConc base plate — 20 µM (interleaved)')
        view.base_plate.set('LowConc base plate — 20 µM (interleaved)')
        view.final_volume.set('20')
        view.base_target.set('25')
        for source in view.color_views.values():
            source.desired_concentration_nm.set('25')
        assert app.base_path.get() == str(PATH)
        assert app._selected_hinge_type() == 'balanced_rigid'
        assert app.base_stock_concentration_nm() == 20000
        view.tile_count.set('1')
        view._tile_count_changed()
        path = tmp_path / 'input.png'
        Image.new('RGB', (6,8), (0,128,255)).save(path)
        view.image_path.set(str(path))
        with patch('rgb_image_view.messagebox.showerror') as error:
            view._load()
            view._prepare()
            error.assert_not_called()
        assert len(view.runs) == 3
        assert all(r['hinge_type'] == 'balanced_rigid' for r in view.runs)
    finally:
        app.destroy()


def test_saved_generic_lowconc_choice_migrates_to_interleaved(tmp_path, monkeypatch):
    import json
    import picklist_app
    for key in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, key, tmp_path / (key + '.json'))
    picklist_app.CONFIG_PATH.write_text(json.dumps({
        'base_plate': 'LowConc base plate — 20 µM', 'hinge_type': 'original'}))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        assert app.base_plate.get() == 'LowConc base plate — 20 µM (interleaved)'
        assert app.base_path.get() == str(PATH)
        assert app._selected_hinge_type() == 'balanced_rigid'
        assert app.base_stock_concentration_nm() == 20000
        app._flush_config()
        saved = json.loads(picklist_app.CONFIG_PATH.read_text())
        assert saved['base_plate'] == app.base_plate.get()
    finally:
        app.destroy()
