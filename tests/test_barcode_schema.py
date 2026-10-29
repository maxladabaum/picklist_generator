from copy import deepcopy
import json
from unittest.mock import patch

from PIL import Image
import pytest

from image_encoding import (CHANNELS, BIT_SITES, default_barcode_schema, validate_barcode_schema,
                            compile_designs, geometry, site_name)


def moved_schema():
    schema = default_barcode_schema()
    schema['name'] = 'Color bit on row 12'
    schema['six_bit'][0]['sites'] = ['H01-12', 'H03-12', 'H05-12']
    return schema


@pytest.mark.parametrize('scheme', ['fixed_4x4', 'tiles_2x2', 'joint_6x3', 'nine_fixed_16x8'])
def test_schema_changes_physical_sites_without_changing_codes_or_image(scheme):
    info = geometry(scheme)
    pixels = [[[255,128,0] for _ in range(info['columns'])] for _ in range(info['rows'])]
    schema = moved_schema()
    if info['bit_count'] == 9:
        schema['nine_bit'][0]['sites'] = ['H01-07', 'H03-07']
    original = compile_designs(pixels,50,{c:1 for c in CHANNELS},scheme)
    changed = compile_designs(pixels,50,{c:1 for c in CHANNELS},scheme,barcode_schema=schema)
    changed_info = geometry(scheme,barcode_schema=schema)
    for before, after in zip(original, changed):
        assert before['bits'] == after['bits']
        assert before['barcode_code'] == after['barcode_code']
        assert before['image_sites'] == after['image_sites']
        for bit, sites in zip(after['bits'],after['barcode_sites']):
            assert all(after['site_fractions'][site_name(*site)] == bit for site in sites)
        assert after['barcode_sites'][0] == [list(site) for site in changed_info['barcode_bits'][0]['sites']]
        assert before['barcode_sites'][0] != after['barcode_sites'][0]


@pytest.mark.parametrize('mutation', ['duplicate','analog','unknown','missing_bit','empty','version'])
def test_reject_invalid_schema(mutation):
    schema = moved_schema()
    if mutation == 'duplicate': schema['six_bit'][1]['sites'] = schema['six_bit'][0]['sites'][:]
    elif mutation == 'analog': schema['six_bit'][0]['sites'] = ['H01-06']
    elif mutation == 'unknown': schema['six_bit'][0]['sites'] = ['H02-12']
    elif mutation == 'missing_bit': schema['six_bit'].pop()
    elif mutation == 'empty': schema['six_bit'][0]['sites'] = []
    else: schema['version'] = 2
    with pytest.raises(ValueError): validate_barcode_schema(schema)


def test_load_save_picklists_reports_invalid_file_and_reload(tmp_path, monkeypatch):
    import picklist_app
    from exhaustive_pdf import ReportSettings, generate_views
    from picklist_core import parse_source2
    for name in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app,name,tmp_path/(name+'.json'))
    app = picklist_app.PicklistApp(); app.withdraw()
    schema_path = tmp_path/'schema.json'
    schema_path.write_text(json.dumps(moved_schema()))
    try:
        view = app.rgb_image_view
        app.base_plate_picker.current(1)
        view.tile_count.set('1'); view._tile_count_changed()
        path = tmp_path/'image.png'; Image.new('RGB',(6,8),'white').save(path)
        view.image_path.set(str(path))
        with patch('rgb_image_view.messagebox.showerror') as error:
            view._load(); view._prepare()
            with patch('rgb_image_view.filedialog.askopenfilename', return_value=str(schema_path)):
                view._load_barcode_schema()
            assert not view.runs
            assert view.barcode_schema == moved_schema()
            view._prepare()
            source_rows, _ = parse_source2(picklist_app.SHEET_DIR/'PAINT_R1_replace.csv')
            wells = {r['Name']: r['Well'] for r in source_rows}
            red = next(r for r in view.runs if r['image_design']['color']=='R1 R')
            replacement_wells = {r['Source Well'] for r in red['picklist'] if r['Source Plate Name']=='SourcePlate7[7]'}
            assert {wells[site_name(row,'12')] for row in ['H01','H03','H05']} <= replacement_wells
            assert not {wells[site_name(*site)] for site in BIT_SITES[0]} & replacement_wells
            assert view.snapshot['barcode_schema'] == moved_schema()
            settings = ReportSettings(str(path),'Stretch',25,{c:1 for c in CHANNELS},10,.5, **view._concentration_options())
            settings.validate()
            report = generate_views(settings,'tiles_1x1',0)
            assert report['geometry']['barcode_bits'][0]['sites'] == [('H01','12'),('H03','12'),('H05','12')]
            with patch('rgb_image_view.filedialog.asksaveasfilename',return_value=str(tmp_path/'saved.json')):
                view._save_barcode_schema()
            assert json.loads((tmp_path/'saved.json').read_text()) == moved_schema()
            error.assert_not_called()
            previous = deepcopy(view.designs)
            schema_path.write_text('{}')
            with patch('rgb_image_view.filedialog.askopenfilename',return_value=str(schema_path)):
                view._load_barcode_schema()
            error.assert_called_once()
            assert view.designs == previous
            assert view.runs
            app._flush_config()
    finally:
        app.destroy()
    reopened = picklist_app.PicklistApp(); reopened.withdraw()
    try:
        assert reopened.rgb_image_view.barcode_schema == moved_schema()
    finally:
        reopened.destroy()
