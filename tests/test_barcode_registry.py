from copy import deepcopy
from unittest.mock import Mock, patch
import json

import pytest
from PIL import Image

from barcode_registry import BarcodeRegistry, record, decode, from_rgb, layout
from image_encoding import default_barcode_schema, compile_designs, CHANNELS
from test_rgb_synthesis import app


def fractions(schema,bits):
    result={s:int(bit) for bit,g in zip(bits,layout(schema,len(bits))) for s in g}
    result.update({s:1 for s in schema.get('always_on_sites',[])})
    return result


def entry(bits='100000',id='run1',schema=None,experiment='pool'):
    schema=schema or default_barcode_schema()
    return record(schema,len(bits),fractions(schema,bits),experiment,id,'test '+id,'Dest',['A01'])


def test_registry_persistence_collision_release_and_experiment_scope(tmp_path):
    path=tmp_path/'registry.json';registry=BarcodeRegistry(path)
    assert registry.reserve([entry()])==1
    assert registry.reserve([entry()])==0
    registry=BarcodeRegistry(path)
    with pytest.raises(ValueError,match='Already used'):registry.check(entry(id='run2'))
    registry.check(entry(id='run2',experiment='separate'))
    registry.release(registry.entries[0])
    assert registry.reserve([entry()],check=False)==0 # Storage sync cannot restore a released reservation.
    registry.check(entry(id='run2'))
    assert [e['action'] for e in registry.state['events']]==['reserve','release']


def test_schema_renaming_reordering_and_fiducials_cannot_hide_collisions(tmp_path):
    registry=BarcodeRegistry(tmp_path/'r.json');registry.reserve([entry()])
    schema=default_barcode_schema();schema['name']='renamed'
    schema['six_bit'][0]['sites'],schema['six_bit'][1]['sites']=schema['six_bit'][1]['sites'],schema['six_bit'][0]['sites']
    schema['always_on_sites']=['H07-12']
    with pytest.raises(ValueError,match='Already used'):registry.check(entry('010000','other',schema))
    schema['six_bit'][0]['sites']=['H09-12']
    with pytest.raises(ValueError,match='Incompatible'):registry.check(entry('010000','other',schema))


def test_invalid_partial_bits_and_missing_fiducials():
    schema=default_barcode_schema();schema['always_on_sites']=['H07-12']
    values=fractions(schema,'100000');values['H01-11']=.5
    with pytest.raises(ValueError,match='Invalid bit'):decode(schema,6,values)
    values['H01-11']=0
    with pytest.raises(ValueError,match='Invalid bit'):decode(schema,6,values)
    values=fractions(schema,'100000');values.pop('H07-12')
    with pytest.raises(ValueError,match='fiducial'):decode(schema,6,values)


def test_import_manifest_and_registry_are_idempotent(tmp_path):
    designs=compile_designs([[[0]*3 for _ in range(6)] for _ in range(8)],50,{c:1 for c in CHANNELS},'tiles_1x1')
    payload={'format':'origami-rgb-image-v2','designs':designs,'runs':[]}
    registry=BarcodeRegistry(tmp_path/'r.json')
    assert registry.import_payload(payload,'pool')==3
    assert registry.import_payload(payload,'pool')==0
    assert registry.import_payload(deepcopy(registry.state),'pool')==0
    with pytest.raises(ValueError,match='Already used'):
        candidate=deepcopy(registry.entries[0]);candidate['id']='manual';registry.check(candidate)
    with pytest.raises(ValueError):registry.import_payload({'not':'a log'},'pool')


def prepare_rgb(app,tmp_path):
    view=app.rgb_image_view
    view.auto_export_templates.set(False);view.optimize_enabled.set(False);view.alignment_enabled.set(False)
    view.tile_count.set('1');view._tile_count_changed()
    path=tmp_path/'image.png';Image.new('RGB',(6,8),(100,100,100)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load();view._prepare();error.assert_not_called()
    return view


def test_rgb_storage_blocks_manual_duplicate_and_unused_code_stores(app,tmp_path):
    rgb=prepare_rgb(app,tmp_path)
    guard=app.barcode_guard
    with patch('rgb_image_view.messagebox.showerror') as error:
        rgb._store();error.assert_not_called()
    assert len(guard.registry.entries)==3
    assert all(item['barcode_record'] for item in app._storage_items())
    guard.use_rgb()
    with pytest.raises(ValueError,match='Already used'):guard.check_manual() # RGB green owns 000000.
    view=next(v for v in app.set_views if v.title=='LowConc PAINT R1')
    view.variables['U-Apt_H09-03_R1'].set(True)
    with patch('picklist_app.messagebox.showerror') as error:
        app.generate(Mock(),Mock());error.assert_called_once()
    assert not app.last_picklist
    guard.select_unused()
    assert guard.check_manual()['bits']=='000001'
    assert view.variables['U-Apt_H09-03_R1'].get() # Existing image selections remain.
    app.destination_wells.set('B01');app.desired.set('100,50,10,1,12,5')
    with patch('picklist_app.messagebox.showerror') as error, patch('picklist_app.simpledialog.askstring',return_value='Manual unique'):
        app.generate(Mock(),Mock());error.assert_not_called()
        app._add_latest_to_storage();error.assert_not_called()
    assert len(guard.registry.entries)==4
    app.storage_state['items']=[]
    guard.sync_storage()
    reloaded=BarcodeRegistry(guard.registry.path)
    candidate=guard.manual_record('new')
    with pytest.raises(ValueError,match='Manual unique'):reloaded.check(candidate)


def test_new_collision_after_preview_blocks_manual_store(app,tmp_path):
    app.barcode_guard.enabled.set(True)
    view=next(v for v in app.set_views if v.title=='LowConc PAINT R1')
    view.variables['U-Apt_H09-03_R1'].set(True)
    app.destination_wells.set('B01');app.desired.set('100,50,10,1,12,5')
    app.generate(Mock(),Mock())
    assert app.last_picklist
    candidate=deepcopy(app.last_barcode_record);candidate['id']='external';candidate['run_name']='Imported collision'
    app.barcode_guard.registry.reserve([candidate])
    with patch('picklist_app.messagebox.showerror') as error, patch('picklist_app.simpledialog.askstring',return_value='Manual duplicate'):
        app._add_latest_to_storage();error.assert_called_once()
    assert not app._storage_items()


def test_fiducial_schema_unused_selection_and_rgb_prepare_collision(app,tmp_path):
    guard=app.barcode_guard
    schema=default_barcode_schema();schema['always_on_sites']=['H07-12']
    guard.apply_schema(schema,6)
    with pytest.raises(ValueError,match='fiducial'):guard.check_manual()
    guard.select_unused()
    assert guard.check_manual()['fractions']['H07-12']==1
    guard.registry.reserve([guard.manual_record('reserved')])
    rgb=app.rgb_image_view
    rgb.auto_export_templates.set(False);rgb.optimize_enabled.set(False);rgb.alignment_enabled.set(False)
    rgb.tile_count.set('1');rgb._tile_count_changed()
    path=tmp_path/'image.png';Image.new('RGB',(6,8),'black').save(path);rgb.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        rgb._load();rgb._prepare();error.assert_called_once()
        assert 'Already used' in error.call_args.args[1]
    assert not rgb.runs


def test_manifest_reimport_of_stored_rgb_keeps_same_reservations(app,tmp_path):
    rgb=prepare_rgb(app,tmp_path)
    rgb._store()
    payload=deepcopy(rgb.snapshot)
    payload['runs']=[{key:run[key] for key in ('run_name','id','source_run_id','destination_plate_name','destination_wells')} for run in rgb.runs]
    guard=app.barcode_guard
    assert guard.registry.import_payload(payload,'Default')==0
    for r in payload['runs']:
        r.pop('id');r.pop('source_run_id')
    assert guard.registry.import_payload(payload,'Default')==0 # Older manifests.
    guard.registry.release(guard.registry.entries[0])
    guard.sync_storage()
    assert guard.registry.entries[0]['released']


def test_rgb_store_rechecks_codes_reserved_after_preparation(app,tmp_path):
    rgb=prepare_rgb(app,tmp_path)
    entry=deepcopy(rgb.runs[0]['barcode_record']);entry['id']='other-run'
    app.barcode_guard.registry.reserve([entry])
    with patch('rgb_image_view.messagebox.showerror') as error:
        rgb._store();error.assert_called_once()
        assert 'Already used' in error.call_args.args[1]
    assert not app._storage_items()


def test_experiment_change_invalidates_prepared_runs(app,tmp_path):
    rgb=prepare_rgb(app,tmp_path)
    assert rgb.runs
    app.barcode_guard.experiment.set('Independent experiment')
    assert not rgb.runs


def test_exhaustion_does_not_change_manual_selections(app):
    guard=app.barcode_guard
    guard.registry.reserve([entry(format(n,'06b'),str(n),experiment='Default') for n in range(64)])
    before=[v.selected() for v in app.set_views]
    with patch('barcode_guard_view.messagebox.showerror') as error:
        guard.select_unused();error.assert_called_once()
        assert 'All codes' in error.call_args.args[1]
    assert [v.selected() for v in app.set_views]==before


def test_loading_single_family_schema_selects_its_bit_count(app):
    guard=app.barcode_guard
    schema=default_barcode_schema();schema.pop('six_bit')
    guard.apply_schema(schema)
    assert guard.count.get()=='9'
    schema=default_barcode_schema();schema.pop('nine_bit')
    guard.apply_schema(schema)
    assert guard.count.get()=='6'


def test_manual_without_barcode_generates_and_stores_without_reservation(app):
    guard = app.barcode_guard
    assert not guard.enabled.get()
    # A saved schema may require fiducials even for an ordinary replacement design.
    schema = default_barcode_schema()
    schema['always_on_sites'] = ['H07-12']
    guard.apply_schema(schema, 6)
    assert guard.enabled.get()
    guard.enabled.set(False)
    view = next(v for v in app.set_views if v.title == 'LowConc PAINT R1')
    view.variables['U-Apt_H01-11_R1'].set(True)  # Only part of a barcode bit.
    app.destination_wells.set('B01')
    app.desired.set('100,50,10,1,12,5')
    with patch('picklist_app.messagebox.showerror') as error, patch(
            'picklist_app.simpledialog.askstring', return_value='No barcode'):
        app._make_picklist()
        assert app.main_tabs.select() == str(app.settings_page)
        app.generate(Mock(), Mock())
        assert app.last_picklist
        assert app.last_barcode_record is None
        app._add_latest_to_storage()
        error.assert_not_called()
    assert len(app._storage_items()) == 1
    assert app._storage_items()[0]['barcode_record'] is None
    assert not guard.registry.entries
    assert app.config['manual_barcode_enabled'] is False
    guard.enabled.set(True)
    with pytest.raises(ValueError):
        guard.check_manual()
    assert not app.last_picklist  # Mode changes invalidate the previous preview.


def test_unused_code_enables_manual_barcode_checks(app):
    guard = app.barcode_guard
    assert not guard.enabled.get()
    guard.select_unused()
    assert guard.enabled.get()
    assert guard.check_manual()['bits'] == '000000'
