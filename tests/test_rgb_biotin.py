import csv
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from picklist_core import parse_source1, parse_source2
from test_rgb_synthesis import app

SHEETS = Path(__file__).resolve().parents[1]/'replacement_sheets'
ADDITIONS = SHEETS/'lowconc_D_biotin_additions.csv'


def test_biotin_inventory_is_collision_free_and_matches_paint_sequences():
    additions, plate = parse_source2(ADDITIONS)
    paint, _ = parse_source2(SHEETS/'PAINT_replace.csv')
    paint = {row['Name']:row for row in paint if row['Name'].startswith('D-Biotin_')}
    assert plate == 'SourcePlate8[8]'
    assert len(additions) == len(paint) == 8
    rgb_rows, _ = parse_source2(SHEETS/'PAINT_R1_replace.csv')
    assert not {r['Replace Well'] for r in additions} & {r['Replace Well'] for r in rgb_rows}
    assert {r['Well'] for r in additions} == {f'P{i}' for i in range(17,25)}
    for row in additions:
        assert row['Sequence'] == paint[row['Name']]['Sequence']
        assert row['Replace Well'] == paint[row['Name']]['Replace Well']
        assert row['Sequence'].startswith('/5Biosg/')
    for filename in ('book_base_20uM.csv','book_base_20uM_interleaved.csv'):
        with (SHEETS/filename).open() as handle:
            next(handle); records=list(csv.DictReader(handle))
        assert len({r['Well Position'] for r in records}) == len(records)
        extras={r['Sequence Name']:r for r in records if r['Usage']=='Supplemental'}
        assert len(extras)==8
        for row in additions:
            extra=extras[row['Name']]
            assert extra['Well Position']==row['Well']
            assert extra['Sequence']==row['Sequence']
            assert extra['Original Well Position']==row['Replace Well']
            assert extra['Nominal Concentration µM']=='20'


def test_inventory_rebuild_preserves_additions_and_rejects_collisions():
    from scripts.build_interleaved_base import add_biotin_inventory
    with (SHEETS/'book_base_20uM_interleaved.csv').open() as handle:
        next(handle); records=list(csv.DictReader(handle))
    base=[row for row in records if row['Usage']!='Supplemental']
    rebuilt=add_biotin_inventory(base)
    assert {row['Well Position']:row for row in rebuilt} == {row['Well Position']:row for row in records}
    with pytest.raises(ValueError,match='overwrite occupied well P17'):
        add_biotin_inventory(base+[dict(base[0], **{'Well Position':'P17'})])


@pytest.mark.parametrize('biotin_target', [25, 100])
@pytest.mark.parametrize('base', ['Base plate — 200 µM','LowConc base plate — 20 µM (original layout)',
                                  'LowConc base plate — 20 µM (interleaved)'])
def test_every_rgb_run_replaces_originals_with_biotin_at_independent_target(app,tmp_path,base,biotin_target):
    view=app.rgb_image_view
    view.auto_export_templates.set(False)
    view.base_plate.set(base)
    view.final_volume.set('200');view.base_target.set('25');view.biotin_target.set(str(biotin_target))
    for color in view.color_views.values():
        color.desired_concentration_nm.set('50')
    view.tile_count.set('1');view._tile_count_changed()
    path=tmp_path/'image.png';Image.new('RGB',(6,8),(128,255,0)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load();view._prepare();error.assert_not_called()
    assert len(view.runs)==3
    hinges = 'balanced_rigid' if 'interleaved' in base else 'original'
    originals,base_plate=parse_source1(Path(view.base_path.get()),hinges)
    by_key={row['Replacement Well']:row['Well'] for row in originals}
    additions,_=parse_source2(ADDITIONS)
    for run in view.runs:
        pick=run['picklist']
        for row in additions:
            matches=[t for t in pick if t['Source Plate Name']=='SourcePlate8[8]' and t['Source Well']==row['Well']]
            assert len(matches)==1
            assert matches[0]['Sample Comments']==row['Sequence']
            assert matches[0]['Transfer Volume']==biotin_target*10
            assert matches[0]['Transfer Volume']*20000/(200*1000)==biotin_target
            assert not any(t['Source Plate Name']==base_plate and t['Source Well']==by_key[row['Replace Well']] for t in pick)
        assert run['d_biotin']['target_nm']==biotin_target
        assert run['selection_panels'][-1]['group']=='D-Biotin'
        assert sum(r['Volume_uL'] for r in run['mixing_recipe'])==pytest.approx(200)
        assert run['mixing_recipe'][0]['Volume_uL']==pytest.approx(sum(r['Transfer Volume'] for r in pick)/1000)
    with patch('rgb_image_view.filedialog.askdirectory',return_value=str(tmp_path)), patch('rgb_image_view.messagebox.showerror') as error:
        view._export();view._store();error.assert_not_called()
    import json
    manifest=json.loads(next(tmp_path.glob('*/image_manifest.json')).read_text())
    assert all(r['d_biotin']['target_nm']==biotin_target for r in manifest['runs'])
    assert all(len(r['d_biotin']['staples'])==8 for r in app._storage_items())


def test_missing_biotin_source_blocks_preparation(app,tmp_path):
    view=app.rgb_image_view
    view.auto_export_templates.set(False)
    view.tile_count.set('1');view._tile_count_changed()
    path=tmp_path/'black.png';Image.new('RGB',(6,8),'black').save(path)
    view.image_path.set(str(path));view._load()
    view.biotin_path.set(str(tmp_path/'missing.csv'))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._prepare();error.assert_called_once()
    assert view.runs==[]
    assert str(view.export_button['state'])=='disabled'


def test_biotin_target_is_independent_persistent_and_invalidates_runs(app,tmp_path):
    view=app.rgb_image_view
    view.auto_export_templates.set(False)
    view.tile_count.set('1');view._tile_count_changed()
    path=tmp_path/'black.png';Image.new('RGB',(6,8),'black').save(path)
    view.image_path.set(str(path));view._load()
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._prepare();error.assert_not_called()
        before=view.runs[0]['picklist']
        base_volume=view.transfer_volume.get()
        view.biotin_target.set('100')
        assert not view.runs
        assert view.transfer_volume.get()==base_volume
        assert str(view.export_button['state'])=='disabled'
        view._prepare();error.assert_not_called()
    after=view.runs[0]['picklist']
    def by_source(rows):
        return {(r['Source Plate Name'],r['Source Well']):r['Transfer Volume'] for r in rows}
    original,updated=by_source(before),by_source(after)
    for key,volume in original.items():
        if key[0]=='SourcePlate8[8]' and key[1] in {f'P{i}' for i in range(17,25)}:
            assert updated[key]==2*volume
        else:
            assert updated[key]==volume
    view.base_target.set('100')
    assert view.biotin_target.get()=='100'
    view.base_target.set('50')
    assert view.biotin_target.get()=='100'
    app._flush_config()
    import json,picklist_app
    assert json.loads(picklist_app.CONFIG_PATH.read_text())['image_run_settings']['biotin_target']=='100'


@pytest.mark.parametrize('target',['0','-1','nan','0.1','10000'])
def test_invalid_or_infeasible_biotin_target_blocks_preparation(app,tmp_path,target):
    view=app.rgb_image_view
    view.auto_export_templates.set(False)
    view.tile_count.set('1');view._tile_count_changed()
    path=tmp_path/'black.png';Image.new('RGB',(6,8),'black').save(path)
    view.image_path.set(str(path));view._load()
    view.biotin_target.set(target)
    view.max_volume.set('100')
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._prepare();error.assert_called_once()
    assert not view.runs
    assert str(view.store_button['state'])=='disabled'


def test_biotin_target_migration_and_reload(tmp_path,monkeypatch):
    import json,picklist_app
    for name in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app,name,tmp_path/(name+'.json'))
    picklist_app.CONFIG_PATH.write_text(json.dumps({'image_run_settings':{'base_target':'25','final_volume':'200'}}))
    app=picklist_app.PicklistApp();app.withdraw()
    try:
        view=app.rgb_image_view
        assert view.biotin_target.get()=='25'
        view.biotin_target.set('100')
        app._flush_config()
    finally:
        app.destroy()
    app=picklist_app.PicklistApp();app.withdraw()
    try:
        assert app.rgb_image_view.biotin_target.get()=='100'
        assert app.rgb_image_view.base_target.get()=='25'
    finally:
        app.destroy()
