"""Integration of standard concentration handling with Mona Lisa RGB runs."""
from copy import deepcopy

import pytest
from image_encoding import prepare_runs, compile_designs, CHANNELS
from test_image_encoding import designs, solid, SETTINGS, MIXING, SHEETS
from app_state import all_384_wells


def test_rgb_targets_follow_base_target_and_actual_stock_concentration():
    item = designs(solid((255, 255, 255)))[0]
    sources = {color: (SHEETS / "PAINT_R1_replace.csv", "RGB") for color in CHANNELS}
    mixing = deepcopy(MIXING)
    mixing['staple'][0] = 100000
    mixing['desired'][1] = 25
    run = prepare_runs([item], SHEETS / "book_base.csv", sources, all_384_wells(), SETTINGS, mixing)[0]
    replacements = [row for row in run['picklist'] if row['Source Plate Name'] == 'RGB']
    assert replacements
    assert all(row['Transfer Volume'] == 125 for row in replacements)
    assert SETTINGS.get('base_concentration_nm') is None


def test_rgb_quantization_rejects_fractional_droplets_and_invalid_base():
    for base, multiplier in ((50, 0.25), (33, 50 / 33)):
        with pytest.raises(ValueError, match='25 nL'):
            compile_designs(solid((128, 128, 128)), base, {color: multiplier for color in CHANNELS})


def test_pdf_and_similarity_settings_keep_rgb_multiplier_semantics(tmp_path):
    from PIL import Image
    from exhaustive_pdf import ReportSettings
    path = tmp_path / 'source.png'
    Image.new('RGB', (8, 8), 'red').save(path)
    settings = ReportSettings(str(path), 'Center crop', 50, {color: 5 for color in CHANNELS}, 7.5, 1.0)
    settings.validate()
    settings.multipliers['R1 R'] = 0.25
    with pytest.raises(ValueError, match='25 nL'):
        settings.validate()


def test_low_concentration_rgb_preserves_molar_splits_and_accounts_for_capacity():
    item = designs(solid((128, 128, 128)))[0]
    sources = {color: (SHEETS / "PAINT_R1_replace.csv", "SourcePlate7[7]") for color in CHANNELS}
    standard = prepare_runs([item], SHEETS / "book_base.csv", sources, all_384_wells(), SETTINGS, MIXING)[0]
    dilute = prepare_runs([item], SHEETS / "book_base.csv", sources, all_384_wells(), SETTINGS, MIXING,
                          source_concentrations_nm={color: 20000 for color in CHANNELS})[0]
    for old, new in zip(standard['picklist'], dilute['picklist']):
        ratio = 10 if old['Source Plate Name'] == 'SourcePlate7[7]' else 1
        assert new['Transfer Volume'] == ratio * old['Transfer Volume']
        assert new['Source Well'] == old['Source Well']
    assert len(dilute['destination_wells']) > len(standard['destination_wells'])
    assert dilute['mixing_recipe'][0]['Volume_uL'] > standard['mixing_recipe'][0]['Volume_uL']


def test_rgb_gui_prepares_from_low_concentration_color_plate(tmp_path, monkeypatch):
    from PIL import Image
    import picklist_app
    from unittest.mock import patch
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + '.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        view = app.rgb_image_view
        app.rgb_image_view.final_volume.set("100")
        path = tmp_path / 'white.png'
        Image.new('RGB', (24, 32), 'white').save(path)
        with patch('rgb_image_view.messagebox.showerror') as error:
            view.image_path.set(str(path))
            view._load()
            # One design makes source identity and volume check independent of
            # whether a full dilute-stock batch fits on one destination plate.
            view.designs = view.designs[:1]
            view._prepare()
            error.assert_not_called()
        assert len(view.runs) == 1
        rows = [r for r in view.runs[0]['picklist'] if r['Source Plate Name'] == 'SourcePlate7[7]']
        assert rows
        # 25 nL original-stock equivalent, 10× stock dilution.
        assert all(r['Transfer Volume'] == 250 for r in rows)
        assert not any(r['Source Plate Name'] == 'SourcePlate6[6]' for r in view.runs[0]['picklist'])
    finally:
        app.destroy()
