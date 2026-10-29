import csv
from copy import deepcopy
from unittest.mock import Mock, patch
from PIL import Image
import picklist_app


def test_rgb_and_manual_runs_persist_and_export_together(tmp_path, monkeypatch):
    for key in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app,key,tmp_path/(key+'.json'))
    app=picklist_app.PicklistApp()
    app.withdraw()
    try:
        app.output_root.set(str(tmp_path))
        rgb=app.rgb_image_view
        rgb.auto_export_templates.set(False)
        rgb.base_plate.set('LowConc base plate — 20 µM (interleaved)')
        rgb.final_volume.set('20')
        rgb.base_target.set('25')
        for source in rgb.color_views.values():
            source.desired_concentration_nm.set('25')
        rgb.tile_count.set('1'); rgb._tile_count_changed()
        image=tmp_path/'rgb.png'
        Image.new('RGB',(6,8),(80,160,240)).save(image)
        rgb.image_path.set(str(image))
        with patch('rgb_image_view.messagebox.showerror') as error:
            rgb._load(); rgb._prepare(); rgb._store()
            error.assert_not_called()
        assert len(app._storage_items())==3
        rgb_items=deepcopy(app._storage_items())
        rgb_wells={w for item in rgb_items for w in item['destination_wells']}
        # A manual run can use a different base plate without changing stored RGB rows.
        app.base_plate.set('Base plate — 200 µM')
        app.destination_wells.set('B01')
        app.desired.set('100,50,10,1,12,5')
        view=next(v for v in app.set_views if v.title=='LowConc PAINT R1')
        name=next(iter(view.variables))
        view.variables[name].set(True)
        app.barcode_guard.use_rgb()
        app.barcode_guard.select_unused()
        with patch('picklist_app.messagebox.showerror') as error, \
             patch('picklist_app.simpledialog.askstring',return_value='Manual test'):
            app.generate(Mock(),Mock())
            assert app.last_picklist
            app._add_latest_to_storage()
            error.assert_not_called()
        assert len(app._storage_items())==4
        assert app._storage_items()[:3]==rgb_items
        manual=app._storage_items()[-1]
        assert not rgb_wells.intersection(manual['destination_wells'])
        assert 'SourcePlate1[1]' in {r['Source Plate Name'] for r in manual['picklist']}
        expected=[dict(r) for item in app._storage_items() for r in item['picklist']]
    finally:
        app.destroy()
    app=picklist_app.PicklistApp()
    app.withdraw()
    try:
        assert len(app._storage_items())==4
        app._select_all_storage()
        output=tmp_path/'combined'
        output.mkdir()
        with patch.object(app,'_shared_export_directory',return_value=output), \
             patch('picklist_app.messagebox.showinfo'), \
             patch('picklist_app.messagebox.showerror') as error:
            app._generate_combined_storage()
            error.assert_not_called()
        with (output/'picklist_combined.csv').open() as handle:
            rows=list(csv.DictReader(handle))
        assert rows==[{k:str(r[k]) for k in picklist_app.PICKLIST_COLUMNS} for r in expected]
        assert len(list(output.glob('mixing_recipe_*.csv')))==4
        assert {r['Source Plate Name'] for r in rows}=={'SourcePlate1[1]','SourcePlate7[7]','SourcePlate8[8]'}
    finally:
        app.destroy()
