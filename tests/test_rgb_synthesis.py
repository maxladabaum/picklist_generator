import json
from unittest.mock import patch

import pytest
from PIL import Image

import picklist_app
from image_encoding import synthesis_base_volume_nl, site_name
from picklist_core import parse_source1, parse_source2


@pytest.mark.parametrize('volume,target,stock,expected', [(100,50,200000,25),(100,50,20000,250),(200,100,200000,100)])
def test_base_transfer_from_final_synthesis(volume, target, stock, expected):
    assert synthesis_base_volume_nl(volume, target, stock) == expected


@pytest.mark.parametrize('values', [(100,25,200000),(0,50,200000),(100,-1,200000),(100,50,float('nan'))])
def test_impossible_base_transfer_is_rejected(values):
    with pytest.raises(ValueError):
        synthesis_base_volume_nl(*values)


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path/(name+'.json'))
    instance = picklist_app.PicklistApp(); instance.withdraw()
    yield instance
    instance.destroy()


def test_requested_concentrations_and_volume_reach_actual_picklist_and_recipe(app, tmp_path):
    view = app.rgb_image_view
    view.auto_export_templates.set(False)
    view.optimize_enabled.set(False)
    view.alignment_enabled.set(False)
    view.final_volume.set('200')
    view.base_target.set('100')
    view.scaffold_target.set('15')
    view.linear_light.set(False)
    view.fit_mode.set('Stretch')
    for source in view.color_views.values():
        source.desired_concentration_nm.set('50')
    view.tile_count.set('1'); view._tile_count_changed()
    path = tmp_path/'image.png'; Image.new('RGB',(6,8),(128,128,128)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load(); view._prepare()
        error.assert_not_called()
    assert view.transfer_volume.get() == '100'
    assert len(view.runs) == 3
    base_rows, base_plate = parse_source1(picklist_app.SHEET_DIR/'book_base.csv')
    replacements, _ = parse_source2(picklist_app.SHEET_DIR/'PAINT_R1_replace.csv')
    base_lookup = {row.get('Replacement Well',row['Well']):row['Well'] for row in base_rows}
    replacement_lookup = {row['Name']:row for row in replacements}
    for run in view.runs:
        transfers = {(row['Source Plate Name'], row['Source Well']):row['Transfer Volume'] for row in run['picklist']}
        replaced = {replacement_lookup[name]['Replace Well'] for name in run['image_design']['site_fractions']}
        replaced.update(row['Replace Well'] for row in run['d_biotin']['staples'])
        for row in base_rows:
            if row.get('Replacement Well',row['Well']) not in replaced:
                assert transfers[base_plate,row['Well']] * 200000 / (200*1000) == pytest.approx(100)
        site = run['image_design']['image_sites'][0]
        replacement = replacement_lookup[site_name(site['row'],site['column'])]
        assert site['replacement_fraction'] == .5
        original_nl = transfers[base_plate,base_lookup[replacement['Replace Well']]]
        replacement_nl = transfers['SourcePlate7[7]',replacement['Well']]
        assert original_nl * 200000 / 200000 == pytest.approx(25)
        assert replacement_nl * 20000 / 200000 == pytest.approx(25)
        recipe = run['mixing_recipe']
        assert sum(row['Volume_uL'] for row in recipe) == pytest.approx(200)
        assert recipe[1]['Volume_uL'] * 400 / 200 == pytest.approx(15)
        assert recipe[0]['Volume_uL'] == pytest.approx(sum(row['Transfer Volume'] for row in run['picklist'])/1000)
    before = [run['picklist'] for run in view.runs]
    view.scaffold_target.set('20')
    assert not view.runs
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._prepare(); error.assert_not_called()
    assert [run['picklist'] for run in view.runs] == before
    assert view.runs[0]['mixing_recipe'][1]['Volume_uL'] == 10
    view.final_volume.set('201')
    assert view.transfer_volume.get() == ''
    assert not view.runs
    assert not view.designs
    assert '25 nL' in view.synthesis_summary.get()


def test_saved_recipe_migrates_but_repeat_multiplier_is_retired(tmp_path, monkeypatch):
    for name in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path/(name+'.json'))
    picklist_app.CONFIG_PATH.write_text(json.dumps({'image_run_settings': {
        'desired_mix':'200,15,1,12,5', 'transfer_volume':'999', 'transfers_per_source':'7'}}))
    app = picklist_app.PicklistApp(); app.withdraw()
    try:
        view = app.rgb_image_view
        assert view.final_volume.get() == '200'
        assert view.scaffold_target.get() == '15'
        assert view.transfer_volume.get() == '50'
        assert 'transfers_per_source' not in view.run_variables
        view._save_run_settings(); app._flush_config()
        saved = json.loads(picklist_app.CONFIG_PATH.read_text())['image_run_settings']
        assert saved['final_volume'] == '200'
        assert 'transfer_volume' not in saved
        assert 'transfers_per_source' not in saved
    finally:
        app.destroy()


def test_rgb_mixing_balances_carryover_and_rejects_infeasible_recipe(app, tmp_path):
    view = app.rgb_image_view
    view.auto_export_templates.set(False)
    view.optimize_enabled.set(False)
    view.alignment_enabled.set(False)
    view.tile_count.set('1'); view._tile_count_changed()
    view.staple_buffer.set('1,10,20')
    view.scaffold.set('400,0.1,3,4')
    view.te_10x.set('0,10,1,2')
    view.magnesium.set('0,0.2,100,1')
    view.sodium.set('0,0.3,2,100')
    view.desired_buffer.set('2,12,5')
    path = tmp_path/'black.png'; Image.new('RGB',(6,8),'black').save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load(); view._prepare(); error.assert_not_called()
    settings = view._mixing_settings()
    for run in view.runs:
        volumes = [row['Volume_uL'] for row in run['mixing_recipe']]
        stocks = [settings[key] for key in ('staple','scaffold','te_10x','magnesium','sodium')]
        for component, target in enumerate((2,12,5), start=1):
            achieved = sum(volume*stock[component] for volume,stock in zip(volumes,stocks))/100
            assert achieved == pytest.approx(target)
    view.desired_buffer.set('2,0,5')  # Mg is already in the collected staple/scaffold stocks.
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._prepare()
        error.assert_called_once()
        assert 'Mg carried' in error.call_args.args[1]
    assert view.runs == []
    assert str(view.export_button['state']) == 'disabled'
    assert str(view.store_button['state']) == 'disabled'


@pytest.mark.parametrize('volume,levels', [(100,[0,1]), (200,[0,.5,1]), (500,[0,.2,.4,.6,.8,1])])
def test_independent_body_target_and_volume_dependent_site_levels(app, tmp_path, volume, levels):
    from image_encoding import rgb_replacement_options
    view = app.rgb_image_view
    view.optimize_enabled.set(False)
    view.alignment_enabled.set(False)
    view.auto_export_templates.set(False)
    view.base_plate.set('LowConc base plate — 20 µM (interleaved)')
    view.final_volume.set(str(volume))
    view.base_target.set('5')
    for source in view.color_views.values():
        source.desired_concentration_nm.set('5')
    view.tile_count.set('1'); view._tile_count_changed()
    assert view._base_transfer_volume() == volume / 4
    assert float(view.base_target.get()) == 5
    assert rgb_replacement_options(volume, 5, 20000, 20000)['fractions'] == pytest.approx(levels)
    path = tmp_path/'fixed.png'; Image.new('RGB',(6,8),(128,128,128)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load(); view._prepare(); error.assert_not_called()
    for run in view.runs:
        assert all(row['Transfer Volume'] == volume / 4 for row in run['picklist'] if row['function'] == 'standard body')
        for site in run['image_design']['image_sites']:
            assert (site['original_nl'] + site['replacement_nl']) * 20000 / (1000 * volume) == pytest.approx(5)
        assert sum(row['Volume_uL'] for row in run['mixing_recipe']) == pytest.approx(volume)
        assert run['mixing_recipe'][0]['Volume_uL'] == pytest.approx(sum(row['Transfer Volume'] for row in run['picklist']) / 1000)


@pytest.mark.parametrize('minimum,increment', [(25,25),(50,25),(30,10)])
def test_configured_transfer_limits_apply_to_all_rgb_sources(app,tmp_path,minimum,increment):
    from picklist_core import valid_transfer
    view = app.rgb_image_view
    view.auto_export_templates.set(False)
    view.optimize_enabled.set(False); view.alignment_enabled.set(False)
    view.base_plate.set('LowConc base plate — 20 µM (interleaved)')
    view.final_volume.set('200'); view.base_target.set('10'); view.biotin_target.set('15')
    view.minimum_transfer_nl.set(str(minimum)); view.transfer_increment_nl.set(str(increment))
    for source in view.color_views.values():
        source.desired_concentration_nm.set('15')
    view.tile_count.set('1');view._tile_count_changed()
    path=tmp_path/'limits.png';Image.new('RGB',(6,8),(70,128,200)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load(); view._prepare(); error.assert_not_called()
    assert view._base_transfer_volume() == 100
    for run in view.runs:
        assert all(valid_transfer(row['Transfer Volume'],minimum,increment) for row in run['picklist'])
        assert all(row['Transfer Volume']==100 for row in run['picklist'] if row['function']=='standard body')
        assert sum(row['Volume_uL'] for row in run['mixing_recipe']) == pytest.approx(200)
    view.biotin_target.set('1')
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._prepare(); error.assert_called_once()
    assert not view.runs


def test_nonuniform_levels_and_optimization_projection():
    from image_encoding import rgb_transfer_grid, set_site_fraction
    grid=rgb_transfer_grid(100,15,10,20000,20000,50,25)
    assert grid['allowed_fractions']==pytest.approx([0,1/3,.5,2/3,1])
    site={'name':'test','replacement_nl':0,'original_nl':150}
    design={**grid,'site_fractions':{}}
    set_site_fraction(design,site,1/6)
    assert site['replacement_fraction'] in grid['allowed_fractions']
    assert site['replacement_nl']+site['original_nl']==150


@pytest.mark.parametrize('alignment',[False,True])
def test_optimizers_preserve_nonuniform_feasible_transfer_levels(alignment):
    import numpy as np
    from image_encoding import compile_designs, CHANNELS
    from gaussian_optimization import optimize_designs
    from gaussian_reconstruction import render_gaussian_overlay
    from alignment_reconstruction import AlignmentModel, AlignmentSettings
    from picklist_core import valid_transfer
    designs=compile_designs([[[70,128,200] for _ in range(6)] for _ in range(8)],100,
        {c:1 for c in CHANNELS},'tiles_1x1',plate_defaults_nm={c:15 for c in CHANNELS},
        base_desired_concentration_nm=10,base_concentration_nm=20000,
        source_concentrations_nm={c:20000 for c in CHANNELS},minimum_transfer_nl=50,transfer_increment_nl=25)
    if alignment:
        for design in designs:
            design['image_sites']=design['image_sites'][:2]
        model=AlignmentModel(designs,AlignmentSettings())
        result,_=model.optimize(designs,np.zeros_like(model.expected(designs).density))
    else:
        result,_=optimize_designs(designs,np.zeros_like(render_gaussian_overlay(designs).density))
    for design in result:
        for site in design['image_sites']:
            assert site['replacement_fraction'] in design['allowed_fractions']
            assert all(valid_transfer(site[key],50,25) for key in ('replacement_nl','original_nl'))
