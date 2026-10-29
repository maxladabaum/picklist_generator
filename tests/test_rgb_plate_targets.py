"""RGB uses plate targets directly and quantizes both unequal-stock transfers."""
from copy import deepcopy
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from image_encoding import CHANNELS, compile_designs, prepare_runs, rgb_transfer_grid
from gaussian_optimization import optimize_designs
from gaussian_reconstruction import render_gaussian_overlay
from alignment_reconstruction import AlignmentModel, AlignmentSettings
from exhaustive_pdf import ReportSettings, generate_views
from test_image_encoding import solid, SETTINGS, MIXING, SHEETS
from app_state import all_384_wells


TARGETS = dict(zip(CHANNELS, (10., 20., 30.)))
STOCKS = {color: 20000. for color in CHANNELS}
OPTIONS = dict(plate_defaults_nm=TARGETS, base_desired_concentration_nm=50.,
               base_concentration_nm=200000., source_concentrations_nm=STOCKS)


def assignments():
    return compile_designs(solid((128, 128, 128)), 250, {c: 999 for c in CHANNELS}, **OPTIONS)


def assert_transfers(designs):
    for design in designs:
        assert design['desired_concentration_nm'] == TARGETS[design['color']]
        assert design['excess_multiplier'] == 1
        for site in design['image_sites']:
            f = site['replacement_fraction']
            assert np.isclose(f * design['fraction_steps'], round(f * design['fraction_steps']))
            assert site['replacement_nl'] % 25 == 0
            assert site['original_nl'] % 25 == 0
            assert np.isclose(site['replacement_nl'], design['full_replacement_nl'] * f)
            assert np.isclose(site['original_nl'], design['full_original_nl'] * (1-f))
            assert np.isclose(site['replacement_nl'] * 20000 + site['original_nl'] * 200000,
                              design['full_original_nl'] * 200000)
        image_names = {site['name'] for site in design['image_sites']}
        assert all(f in (0, 1) for name, f in design['site_fractions'].items() if name not in image_names)


def test_targets_ignore_legacy_excess_and_quantize_molar_fractions():
    designs = assignments()
    assert_transfers(designs)
    assert {d['color']: d['fraction_steps'] for d in designs} == dict(zip(CHANNELS, (2,4,6)))
    assert designs[0]['image_sites'][0]['replacement_nl'] == 250
    assert designs[0]['image_sites'][0]['original_nl'] == 25
    assert designs == compile_designs(solid((128,128,128)),250,{c:1 for c in CHANNELS},**OPTIONS)


def test_picklists_match_concentration_aware_site_volumes():
    from picklist_core import parse_source2
    sources = {c: (SHEETS/'PAINT_R1_replace.csv','SourcePlate7[7]') for c in CHANNELS}
    rows, _ = parse_source2(SHEETS/'PAINT_R1_replace.csv')
    wells = {r['Name']: r['Well'].upper() for r in rows}
    designs = assignments()[::16]
    runs = prepare_runs(designs,SHEETS/'book_base.csv',sources,all_384_wells(),SETTINGS,MIXING,
                        source_concentrations_nm=STOCKS)
    for design, run in zip(designs, runs):
        volumes = {r['Source Well']: r['Transfer Volume'] for r in run['picklist']
                   if r['Source Plate Name']=='SourcePlate7[7]'}
        for site in design['image_sites']:
            assert volumes.get(wells[site['name']],0) == site['replacement_nl']
    changed = deepcopy(MIXING)
    changed['desired'][1] = 25
    with pytest.raises(ValueError,match='regenerate'):
        prepare_runs(designs,SHEETS/'book_base.csv',sources,all_384_wells(),SETTINGS,changed)


def test_both_optimizers_preserve_unequal_stock_transfer_grid():
    designs = assignments()
    baseline = render_gaussian_overlay(designs,10,.5)
    target = np.clip(baseline.density * baseline.metadata['linear_rgb_gain'] * .7,0,1)
    optimized, _ = optimize_designs(designs,target,10,.5)
    assert_transfers(optimized)
    model = AlignmentModel(designs,AlignmentSettings(translation_x_nm=.2,translation_y_nm=.2,rotation_deg=.1),10,.5)
    target = np.clip(model.expected(designs).density * model.gain * .7,0,1)
    aligned, _ = model.optimize(designs,target)
    assert_transfers(aligned)
    assert_transfers(designs)


def test_reports_share_targets_and_record_stocks(tmp_path):
    image = tmp_path/'image.png'
    Image.new('RGB',(24,32),(128,128,128)).save(image)
    settings = ReportSettings(str(image),'Stretch',250,{c:999 for c in CHANNELS},10,.5,
                              linear_light=False,**OPTIONS)
    settings.validate()
    from image_encoding import compile_designs as compile_real
    with patch('exhaustive_pdf.compile_designs',wraps=compile_real) as compile_call:
        generate_views(settings,'fixed_4x4',0,score_only=True)
    assert compile_call.call_args.kwargs['plate_defaults_nm'] == TARGETS
    assert compile_call.call_args.kwargs['source_concentrations_nm'] == STOCKS


def test_impossible_binary_barcode_targets_fail_explicitly():
    with pytest.raises(ValueError,match='both 0% and 100%'):
        rgb_transfer_grid(25,10,50,200000,20000)
    for invalid in (0,-1,float('nan')):
        with pytest.raises(ValueError):
            rgb_transfer_grid(250,invalid,50,200000,20000)


@pytest.fixture
def rgb_app(tmp_path, monkeypatch):
    import picklist_app
    for key in ('CONFIG_PATH','PLATE_STATE_PATH','STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app,key,tmp_path/(key+'.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    app.rgb_image_view.final_volume.set('1000')
    for color, view in app.rgb_image_view.color_views.items():
        view.desired_concentration_nm.set(str(TARGETS[color]))
    yield app
    app.destroy()


def test_gui_plate_defaults_recompute_invalidate_and_export_snapshot(rgb_app,tmp_path):
    app=rgb_app; view=app.rgb_image_view
    path=tmp_path/'white.png'; Image.new('RGB',(24,32),'white').save(path)
    view.image_path.set(str(path)); view._load()
    assert_transfers(view.designs)
    view.runs=[{'old':'prepared'}]
    view.color_views['R1 R'].desired_concentration_nm.set('20')
    assert view.runs == []
    assert all(d['desired_concentration_nm']==20 for d in view.designs if d['color']=='R1 R')
    view.designs=view.designs[:1]
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._prepare()
        error.assert_not_called()
    assert view.runs[0]['image_design']['desired_concentration_nm']==20
    with patch('rgb_image_view.filedialog.asksaveasfilename',return_value=str(tmp_path/'report.pdf')), \
         patch('exhaustive_pdf.start_export_dialog') as start:
        view.exhaustive_pdf_button.invoke()
    settings=start.call_args.args[2]
    assert settings.plate_defaults_nm['R1 R']==20
    assert all(v==1 for v in settings.multipliers.values())
    view.color_views['R1 R'].desired_concentration_nm.set('30')
    assert settings.plate_defaults_nm['R1 R']==20
    assert view.runs==[]
    app.rgb_image_view.final_volume.set('100')
    assert view.designs == []  # These targets need smaller than one original-stock droplet.
    for source in view.color_views.values():
        source.desired_concentration_nm.set('50')
    assert len(view.designs)==48
    assert all(d['full_original_nl']==25 for d in view.designs)
    assert not settings.plate_relative_transfers


def test_rgb_ignores_manual_mixing_fields_for_preview_report_and_picklist(rgb_app, tmp_path):
    app = rgb_app
    view = app.rgb_image_view
    app.rgb_image_view.final_volume.set('100')
    for color_view in view.color_views.values():
        color_view.desired_concentration_nm.set('50')
    view.tile_count.set('1')
    view._tile_count_changed()
    path = tmp_path / 'rgb.png'
    Image.new('RGB', (6, 8), (255, 0, 128)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load()
        before = deepcopy(view.designs)
        assert len(before) == 3
        assert all(d['full_original_nl'] == 25 and d['full_replacement_nl'] == 250 for d in before)
        view._prepare()
        original_picklists = [deepcopy(r['picklist']) for r in view.runs]
        original_recipes = [deepcopy(r['mixing_recipe']) for r in view.runs]
        for name in ('desired', 'staple', 'scaffold', 'magnesium', 'sodium', 'te_10x'):
            getattr(app, name).set('invalid manual value')
        view._volumes_changed()
        assert view.designs == before
        view._prepare()
        assert [r['picklist'] for r in view.runs] == original_picklists
        assert [r['mixing_recipe'] for r in view.runs] == original_recipes
        settings = ReportSettings(str(path), 'Stretch', 25, {c:1 for c in CHANNELS}, 10, .5,
                                  **view._concentration_options())
        settings.validate()
        with patch('exhaustive_pdf.compile_designs', wraps=compile_designs) as call:
            generate_views(settings, 'tiles_1x1', 0, score_only=True)
        assert call.call_args.kwargs['plate_relative_transfers'] is False
        error.assert_not_called()
