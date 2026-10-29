import json
from copy import deepcopy
from unittest.mock import patch

from PIL import Image
import pytest

from picklist_core import HINGE_TYPES, parse_source1
from picklist_app import SHEET_DIR


@pytest.mark.parametrize('hinge', HINGE_TYPES)
def test_dilute_plate_preserves_wells_sequences_and_hinges(hinge):
    original, original_name = parse_source1(SHEET_DIR / 'book_base.csv', hinge)
    dilute, dilute_name = parse_source1(SHEET_DIR / 'book_base_20uM.csv', hinge)
    assert original == dilute
    assert original_name == 'SourcePlate1[1]'
    assert dilute_name == 'SourcePlate8[8]'


def test_independent_rgb_selector_updates_preview_picklist_report_and_saved_choice(tmp_path, monkeypatch):
    import picklist_app
    from exhaustive_pdf import ReportSettings
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + '.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        view = app.rgb_image_view
        view.tile_count.set('1')
        view._tile_count_changed()
        path = tmp_path / 'image.png'
        Image.new('RGB', (6, 8), (255, 0, 128)).save(path)
        view.image_path.set(str(path))
        with patch('rgb_image_view.messagebox.showerror') as error:
            view._load()
            assert all(d['full_replacement_nl'] == 250 for d in view.designs)
            view._prepare()
            assert view.runs
            view.base_plate_picker.current(1)
            view.final_volume.set('20')
            view.base_target.set('25')
            view.biotin_target.set('25')
            for source in view.color_views.values():
                source.desired_concentration_nm.set('25')
            view.base_plate_picker.event_generate('<<ComboboxSelected>>')
            assert app.base_plate_picker.get() != view.base_plate_picker.get()
            assert not view.runs
            assert view.base_stock_concentration_nm() == 20000
            assert app.base_stock_concentration_nm() == 200000
            assert view.base_path.get().endswith('book_base_20uM.csv')
            assert all(v.base_concentration_nm == 200000 for v in app.set_views)
            assert app.staple.get().startswith('200000,')
            assert all(d['full_original_nl'] == d['full_replacement_nl'] == 25 for d in view.designs)
            view._prepare()
            assert len(view.runs) == 3
            for run in view.runs:
                assert {r['Source Plate Name'] for r in run['picklist']} <= {'SourcePlate8[8]', 'SourcePlate7[7]'}
                assert all(r['Transfer Volume'] == 25 for r in run['picklist'])
                assert abs(sum(r['Volume_uL'] for r in run['mixing_recipe']) - 20) < 1e-6
            settings = ReportSettings(str(path), 'Stretch', 25, {c:1 for c in view.color_views},10,.5,
                                      **view._concentration_options())
            settings.validate()
            assert settings.base_concentration_nm == 20000
            saved_designs = deepcopy(view.designs)
            app._flush_config()
            assert json.loads(picklist_app.CONFIG_PATH.read_text())['base_plate'] == app.base_plate.get()
            # Manual edits must leave the RGB preview and prepared runs untouched.
            app.base_plate_picker.current(2)
            assert view.base_plate_picker.current() == 1
            assert view.runs
            assert view.designs == saved_designs
            app._flush_config()
            error.assert_not_called()
    finally:
        app.destroy()
    reopened = picklist_app.PicklistApp()
    reopened.withdraw()
    try:
        assert reopened.base_stock_concentration_nm() == 20000
        assert reopened.base_path.get().endswith('book_base_20uM_interleaved.csv')
        assert reopened.rgb_image_view.base_path.get().endswith('book_base_20uM.csv')
        assert reopened.rgb_image_view.base_plate_picker.current() == 1
    finally:
        reopened.destroy()
