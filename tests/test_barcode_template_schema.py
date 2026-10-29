from copy import deepcopy
from unittest.mock import patch
import pytest
from PIL import Image
from barcode_template_schema import from_template, to_template
from image_encoding import default_barcode_schema, site_name

@pytest.mark.parametrize('count', [6,7,9])
def test_round_trip_and_coordinates(count):
    original=default_barcode_schema()
    original['always_on_sites']=['H07-12','H09-12']
    model=to_template(original,count)
    if count==6:
        assert model['logical_bits'][0]['physical_sites']==[[1,11],[2,11],[3,11]]
    assert model['alignment_groups'][0]['physical_sites']==[[4,12],[5,12]]
    model['logical_bits'].reverse()
    result, actual=from_template({'logical_model':model},original)
    assert actual==count
    for key in ('six_bit','seven_bit','nine_bit','always_on_sites'): assert result[key]==original[key]

def test_invalid_templates():
    model=to_template(default_barcode_schema(),6)
    bad=deepcopy(model); bad['physical_columns']=8
    with pytest.raises(ValueError): from_template(bad)
    bad=deepcopy(model); bad['logical_bits'][0]['label']='other'
    with pytest.raises(ValueError,match='Name every'): from_template(bad)
    bad=deepcopy(model); bad['logical_bits'].pop()
    with pytest.raises(ValueError,match='exactly 6, 7, or 9'): from_template(bad)

def test_visual_edit_apply_and_saved_file(tmp_path,monkeypatch):
    import picklist_app
    for name in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app,name,tmp_path/(name+'.json'))
    app=picklist_app.PicklistApp(); app.withdraw()
    try:
        view=app.rgb_image_view
        view.tile_count.set('1'); view._tile_count_changed()
        path=tmp_path/'black.png'; Image.new('RGB',(6,8),'black').save(path)
        with patch('rgb_image_view.messagebox.showerror') as error:
            view.image_path.set(str(path)); view._load(); view._prepare()
            assert view.runs
            view._edit_barcode_template()
            assert app.main_tabs.select()==str(app.template_page)
            editor=app.template_view
            assert editor._built_shape==(8,12)
            editor.selected_group_id='bit_1'; editor.selected_group_ids=['bit_1']
            editor.group_name_var.set('Bit 1'); editor.group_role_var.set('Digital bit')
            for site,var in editor.site_variables.items(): var.set(site in {(0,11),(1,11),(2,11)})
            editor._update_group()
            editor.group_name_var.set('Fiducials'); editor.group_role_var.set('Alignment-only')
            for site,var in editor.site_variables.items(): var.set(site in {(3,11),(4,11)})
            editor._create_group()
            editor._apply_to_rgb()
            assert app.main_tabs.select()==str(app.rgb_image_page)
            assert not view.runs
            assert view.barcode_schema['six_bit'][0]['sites']==['H01-12','H03-12','H05-12']
            assert view.barcode_schema['always_on_sites']==['H07-12','H09-12']
            assert all(d['site_fractions'][site_name('H07','12')]==1 for d in view.designs)
            view._prepare(); assert view.runs
            saved=tmp_path/'template.json'
            with patch('picklist_app.filedialog.asksaveasfilename',return_value=str(saved)):
                editor._save_logical_schema()
            expected=deepcopy(view.barcode_schema)
            view._apply_barcode_schema(default_barcode_schema())
            with patch('rgb_image_view.filedialog.askopenfilename',return_value=str(saved)):
                view._load_barcode_schema()
            assert view.barcode_schema==expected
            error.assert_not_called()
    finally:
        app.destroy()

def test_six_bit_template_ignores_unused_nine_bit_sites(tmp_path,monkeypatch):
    import picklist_app
    from image_encoding import geometry, compile_designs, CHANNELS, ENCODING_SCHEMES
    model=to_template(default_barcode_schema(),6)
    model['alignment_groups']=[dict(id='align',label='align',physical_sites=[[1,12],[7,12],[8,12]])]
    schema,count=from_template(model)
    assert count==6
    assert schema['always_on_sites']==['H01-12','H13-12','H15-12']
    geometry('tiles_1x1',barcode_schema=schema)
    designs=compile_designs([[[0,0,0] for _ in range(6)] for _ in range(8)],25,
                           {c:1 for c in CHANNELS},'tiles_1x1',barcode_schema=schema)
    assert all(d['site_fractions'][site_name('H01','12')]==1 for d in designs)
    with pytest.raises(ValueError,match='nine_bit'):
        geometry('nine_fixed_16x8',barcode_schema=schema)
    bad=deepcopy(model); bad['alignment_groups'][0]['physical_sites']=[[1,11]]
    with pytest.raises(ValueError,match='six_bit'):
        from_template(bad)
    for name in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app,name,tmp_path/(name+'.json'))
    app=picklist_app.PicklistApp(); app.withdraw()
    try:
        view=app.rgb_image_view
        with patch('rgb_image_view.messagebox.showerror') as error:
            view._apply_schema_payload(model)
            error.assert_not_called()
            previous=view._scheme_id()
            view.runs=[{'prepared':True}]
            view.scheme.set(ENCODING_SCHEMES['nine_fixed_16x8'].label)
            error.assert_called_once()
            assert view._scheme_id()==previous
            assert view.runs==[{'prepared':True}]
            assert view.barcode_schema['always_on_sites']==schema['always_on_sites']
    finally:
        app.destroy()
