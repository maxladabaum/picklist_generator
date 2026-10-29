from pathlib import Path
from unittest.mock import patch
from PIL import Image
import picklist_app


def test_rgb_targets_are_independent_and_persist(tmp_path, monkeypatch):
    for key in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, key, tmp_path / (key + '.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        assert {v.title for v in app.set_views} == {
            'Yaritza Extensions', 'Aptamers', 'Soyeon MB', 'miRNA', 'MB',
            'PAINT P1', 'PAINT R1', 'LowConc PAINT R1',
        }
        for manual_view in app.set_views:
            assert Path(manual_view.path_var.get()).is_file()
            if manual_view.title not in picklist_app.PAINT_REPLACEMENT_TABS:
                assert manual_view.desired_concentration_nm.get() == '50'
        view = app.rgb_image_view
        assert set(view.color_views) == {'R1 R','R1 G','R1 B'}
        view.auto_export_templates.set(False)
        app.output_root.set(str(tmp_path))
        view.color_views['R1 R'].desired_concentration_nm.set('30')
        manual = next(v for v in app.set_views if v.title == 'LowConc PAINT R1')
        assert manual.desired_concentration_nm.get() == '25'
        view.tile_count.set('1')
        view._tile_count_changed()
        path = tmp_path / 'input.png'
        Image.new('RGB', (6,8), (120,200,80)).save(path)
        view.image_path.set(str(path))
        with patch('rgb_image_view.messagebox.showerror') as error:
            view._load()
            view._prepare()
            error.assert_not_called()
        assert len(view.runs) == 3
        assert next(r for r in view.runs if r['image_design']['color']=='R1 R')['image_design']['desired_concentration_nm'] == 30
        app._flush_config()
    finally:
        app.destroy()
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        assert float(app.rgb_image_view.color_views['R1 R'].desired_concentration_nm.get()) == 30
    finally:
        app.destroy()
