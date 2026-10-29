"""RGB run settings are owned by RGB, including allocation and mixing."""
import json
from copy import deepcopy
from unittest.mock import patch

from PIL import Image
import pytest

import picklist_app
from picklist_core import HINGE_TYPES


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + '.json'))
    picklist_app.CONFIG_PATH.write_text(json.dumps({
        'destination_plate_name': 'Manual plate', 'max_destination_volume_uL': 99,
        'transfers_per_source': 7, 'base_plate': 'LowConc base plate — 20 µM (interleaved)',
    }))
    instance = picklist_app.PicklistApp()
    instance.withdraw()
    yield instance
    instance.destroy()


def test_rgb_allocation_mixing_and_manual_edits_are_independent(app, tmp_path):
    view = app.rgb_image_view
    assert view.max_volume.get() == '12.5'
    assert view.destination_plate.get() == 'Destination[1]'
    assert 'transfers_per_source' not in view.run_variables
    assert view.base_stock_concentration_nm() == 200000
    view.auto_export_templates.set(False)
    view.destination_plate.set('RGB plate')
    view.max_volume.set('1')
    view.source_plate_type.set('RGB_SOURCE_TYPE')
    view.hinge_type.set(HINGE_TYPES['balanced_rigid'])
    view.start_well.set('B01')
    view.final_volume.set('100')
    view.base_target.set('50')
    view.magnesium.set('0,0,200,0')
    view.replacement_plate.set('RGB source')
    view.tile_count.set('1'); view._tile_count_changed()
    path = tmp_path / 'rgb.png'
    Image.new('RGB', (6, 8), (255, 128, 0)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load(); view._prepare()
        error.assert_not_called()
        runs = deepcopy(view.runs)
        designs = deepcopy(view.designs)
        assert runs[0]['destination_wells'][0] == 'B01'
        assert len(runs[0]['destination_wells']) > 1
        for run in runs:
            assert run['destination_plate_name'] == 'RGB plate'
            assert run['hinge_type'] == 'balanced_rigid'
            totals = {}
            for row in run['picklist']:
                assert row['Source Plate Type'] == 'RGB_SOURCE_TYPE'
                well = row['Destination Well']
                totals[well] = totals.get(well, 0) + row['Transfer Volume']
            assert max(totals.values()) <= 1000
            assert sum(r['Volume_uL'] for r in run['mixing_recipe']) == pytest.approx(100)
            assert run['mixing_recipe'][3] == {'Reagent': 'Mg (200 mM)', 'Volume_uL': 6.0}
        assert any(row['Source Plate Name'] == 'RGB source' for row in runs[0]['picklist'])
        for key in ('transfer_volume', 'base_path', 'destination_plate', 'source_plate_type',
                    'max_volume', 'transfers_per_source', 'hinge_type', 'staple', 'scaffold', 'desired'):
            getattr(app, key).set('invalid manual value')
        assert view.runs == runs
        assert view.designs == designs
        view._prepare()
        error.assert_not_called()
        assert [r['picklist'] for r in view.runs] == [r['picklist'] for r in runs]
        assert [r['mixing_recipe'] for r in view.runs] == [r['mixing_recipe'] for r in runs]
        view.max_volume.set('2')
        assert view.runs == []
        assert not view.picklist_tree.get_children()
        view._prepare()
        error.assert_not_called()
        assert len(view.runs[0]['destination_wells']) < len(runs[0]['destination_wells'])


def test_rgb_settings_persist_separately(app):
    view = app.rgb_image_view
    view.destination_plate.set('Saved RGB')
    view.max_volume.set('7.5')
    view.start_well.set('C02')
    view.scaffold.set('500,0.1,0,0')
    view.replacement_stock.set('40000')
    view.output_root.set('rgb_output')
    app._flush_config()
    reopened = picklist_app.PicklistApp()
    reopened.withdraw()
    try:
        rgb = reopened.rgb_image_view
        assert rgb.destination_plate.get() == 'Saved RGB'
        assert rgb.max_volume.get() == '7.5'
        assert rgb.start_well.get() == 'C02'
        assert rgb.scaffold.get() == '500,0.1,0,0'
        assert rgb.color_views['R1 R'].concentration_nm == 40000
        assert rgb.output_root.get() == 'rgb_output'
        assert reopened.destination_plate.get() == 'Manual plate'
        assert float(reopened.max_volume.get()) == 99
    finally:
        reopened.destroy()
