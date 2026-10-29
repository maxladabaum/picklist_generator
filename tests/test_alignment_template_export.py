from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
from PIL import Image
import pytest

from alignment_template_export import export_alignment_templates
from image_encoding import CHANNELS, compile_designs, default_barcode_schema, geometry


def batch(fiducials=True):
    schema=default_barcode_schema()
    schema['always_on_sites']=['H07-12','H09-12'] if fiducials else []
    info=geometry('fixed_4x4',barcode_schema=schema)
    designs=compile_designs([[[128,0,255] for _ in range(24)] for _ in range(32)],
                           25,{c:1 for c in CHANNELS},barcode_schema=schema)
    snapshot=dict(batch_id='test',designs=designs,barcode_schema=schema,geometry=info)
    runs=[dict(run_name=d['name'],destination_plate_name='Destination[1]',destination_wells=['A01']) for d in designs]
    return snapshot,runs


def test_export_loads_in_paint_analysis_and_preserves_exact_bit_states(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]/'paint_analysis'))
    from paint_analysis_gui import load_custom_template_metadata, logical_bit_model_from_metadata, exact_digital_template_matches
    snapshot,runs=batch()
    before=deepcopy(snapshot)
    folder=tmp_path/'templates'
    result=export_alignment_templates(folder,snapshot,runs)
    assert result['exported_count']==48 and result['skipped_count']==0
    assert snapshot==before
    assert len(list((folder/'barcodes').glob('*.png')))==48
    for entry,design in zip(result['templates'],snapshot['designs']):
        metadata=load_custom_template_metadata(folder/entry['png'])
        assert metadata['image_mirrored_x'] is True
        assert metadata['pixel_size_x_nm'] > 0
        model=logical_bit_model_from_metadata(metadata)
        assert list(model['active_bits'])==[bool(bit) for bit in design['bits']]
        assert metadata['selected_site_count']==2+3*sum(design['bits'])
        states=np.array([design['bits'],[1-design['bits'][0],*design['bits'][1:]]],dtype=float)
        params=dict(logical_model=model,digital_pixel_ids=model['bit_ids'],digital_pixel_probabilities=states,
                    digital_group_localization_evidence=np.ones_like(states),digital_group_prominences=np.ones_like(states))
        assert exact_digital_template_matches(params,2).tolist()==[True,False]
        with Image.open(folder/entry['png']) as image:
            assert json.loads(image.info['paint_analysis_template'])==json.loads((folder/entry['metadata']).read_text())
    fiducial=load_custom_template_metadata(folder/result['fiducial_templates'][0])
    assert fiducial['selected_site_count']==2
    assert not any(logical_bit_model_from_metadata(fiducial)['active_bits'])


def test_all_off_without_fiducials_is_explicitly_skipped(tmp_path):
    snapshot,runs=batch(False)
    result=export_alignment_templates(tmp_path/'templates',snapshot,runs)
    assert result['exported_count']==47 and result['skipped_count']==1
    skipped=[e for e in result['templates'] if e['png'] is None]
    assert skipped[0]['barcode_code']==0 and 'no fiducials' in skipped[0]['reason']
    assert not result['fiducial_templates']


def test_failed_export_is_atomic_and_existing_bundle_is_preserved(tmp_path):
    snapshot,runs=batch()
    folder=tmp_path/'templates'
    with patch('alignment_template_export.save_barcode_template',side_effect=OSError('disk full')):
        with pytest.raises(OSError): export_alignment_templates(folder,snapshot,runs)
    assert list(tmp_path.iterdir())==[]
    folder.mkdir(); (folder/'keep.txt').write_text('keep')
    with pytest.raises(ValueError): export_alignment_templates(folder,snapshot,runs)
    assert (folder/'keep.txt').read_text()=='keep'


def test_prepare_auto_export_and_saved_batch(tmp_path,monkeypatch):
    import picklist_app
    for key in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app,key,tmp_path/(key+'.json'))
    app=picklist_app.PicklistApp(); app.withdraw()
    try:
        view=app.rgb_image_view
        app.output_root.set(str(tmp_path/'outputs'))
        app.base_plate_picker.current(1)
        view.tile_count.set('1'); view._tile_count_changed()
        schema=default_barcode_schema(); schema['always_on_sites']=['H07-12']
        view._apply_barcode_schema(schema)
        source=tmp_path/'image.png'; Image.new('RGB',(6,8),'black').save(source)
        with patch('rgb_image_view.messagebox.showerror') as error:
            view.image_path.set(str(source)); view._load(); view._prepare()
            directory=view.alignment_template_directory
            assert directory.is_dir()
            assert len(list((directory/'barcodes').glob('*.png')))==3
            assert all(not plate.get('wells') for plate in app.plate_state.get('plates',{}).values())
            with patch.object(app,'_open_path') as opened:
                view.open_templates_button.invoke()
                opened.assert_called_once_with(directory)
            saved=tmp_path/'saved'; saved.mkdir()
            with patch('rgb_image_view.filedialog.askdirectory',return_value=str(saved)):
                view._export()
            assert view.saved
            assert len(list(saved.glob('*/alignment_templates/barcodes/*.png')))==3
            view._invalidate()
            assert view.alignment_template_directory is None
            assert str(view.open_templates_button['state'])=='disabled'
            assert directory.exists()
            view.auto_export_templates.set(False); view._save_template_export_preference()
            app.barcode_guard.experiment.set('Separate template test batch')
            view._prepare()
            assert view.runs and view.alignment_template_directory is None
            error.assert_not_called()
    finally:
        app.destroy()
