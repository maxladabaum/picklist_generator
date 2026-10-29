from copy import deepcopy
from itertools import combinations
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from image_encoding import (IMAGE_COLUMNS, ROWS, CHANNELS, active_columns, bin_image,
    compile_designs, geometry, reconstruct, site_name, physical_reference_raster)
from gaussian_reconstruction import render_gaussian_overlay
from gaussian_optimization import optimize_designs
from alignment_reconstruction import AlignmentModel, AlignmentSettings

SUBSETS = [list(c) for n in range(1, 7) for c in combinations(IMAGE_COLUMNS, n)]


@pytest.mark.parametrize('columns', SUBSETS)
def test_active_sites_preserve_barcodes_and_cover_every_pixel(columns):
    info = geometry('tiles_2x2', active_image_columns=columns)
    pixels = [[[255, 0, 255] for _ in range(info['columns'])] for _ in range(info['rows'])]
    designs = compile_designs(pixels, 25, {c: 1 for c in CHANNELS}, 'tiles_2x2', active_image_columns=columns)
    assert len(designs) == 12
    assert info['columns'] == 2 * len(columns)
    assert info['rows'] == 16
    assert info['field_width_nm'] == pytest.approx(2 * (int(columns[0])-int(columns[-1])+1)*9.9)
    seen = set()
    for design in designs:
        assert len(design['image_sites']) == 8 * len(columns)
        for site in design['image_sites']:
            assert site['column'] in columns
            identity = (design['channel'], site['pixel_x'], site['pixel_y'])
            assert identity not in seen
            seen.add(identity)
        for column in set(IMAGE_COLUMNS)-set(columns):
            assert all(site_name(row, column) not in design['site_fractions'] for row in ROWS)
        code = next(c for c in geometry('tiles_2x2')['codebook'] if c['color']==design['color'] and c['x_shift']==design['x_shift'] and c['y_shift']==design['y_shift'])
        assert design['bits'] == code['bits']
    assert reconstruct(designs) == pixels


@pytest.mark.parametrize('scheme', ['fixed_4x4', 'tiles_2x2', 'sweep_joint_7x3_tiles_2x2'])
@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
@pytest.mark.parametrize('linear', [False, True])
def test_binning_physical_raster_and_sparse_gap(tmp_path, scheme, rotation, linear):
    columns = ['06', '05', '03', '02', '01']
    path = tmp_path / 'image.png'
    Image.new('RGB', (60, 80), 'white').save(path)
    pixels = bin_image(path, 'Stretch', rotation, scheme, linear_light=linear, active_image_columns=columns)
    info = geometry(scheme, rotation, columns)
    assert np.asarray(pixels).shape == (info['rows'], info['columns'], 3)
    assert np.all(np.asarray(pixels) == 255)
    assert max(np.diff(info['pixel_centers_x_nm'])) > 9.9
    raster = physical_reference_raster(Image.fromarray(np.asarray(pixels, dtype='uint8')), scheme, rotation, columns)
    assert raster.width/raster.height == pytest.approx(info['display_field_width_nm']/info['display_field_height_nm'])
    assert np.all(np.asarray(raster) == 255)


def test_optimization_and_alignment_use_reduced_canvas():
    columns = IMAGE_COLUMNS[:-1]
    pixels = [[[128, 255, 0] for _ in columns] for _ in ROWS]
    designs = compile_designs(pixels, 50, {c: 1 for c in CHANNELS}, 'tiles_1x1', active_image_columns=columns)
    baseline = render_gaussian_overlay(designs, 10, .5)
    target = np.zeros_like(baseline.density)
    optimized, _ = optimize_designs(designs, target, 10, .5)
    model = AlignmentModel(designs, AlignmentSettings(), 10, .5)
    aligned, _ = model.optimize(designs, np.zeros_like(model.expected(designs).density))
    assert model.info['columns'] == 5
    for items in (optimized, aligned):
        assert all(len(d['image_sites']) == 40 for d in items)
        assert all(d['active_image_columns'] == columns for d in items)
        assert [d['bits'] for d in items] == [d['bits'] for d in designs]


def test_ui_canvas_picklists_reports_and_reload(tmp_path, monkeypatch):
    import picklist_app
    from picklist_core import parse_source1, parse_source2
    from exhaustive_pdf import ReportSettings, generate_views
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path/(name+'.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        view = app.rgb_image_view
        view.base_plate_picker.current(1)
        view.final_volume.set('20')
        view.base_target.set('25')
        for source in view.color_views.values():
            source.desired_concentration_nm.set('25')
        view.tile_count.set('1'); view._tile_count_changed()
        path = tmp_path/'white.png'; Image.new('RGB',(40,50),'white').save(path)
        with patch('rgb_image_view.messagebox.showerror') as error:
            view.image_path.set(str(path)); view._load(); view._prepare()
            before_bits = [d['bits'] for d in view.designs]
            assert view.runs
            view.active_row_vars['01'].set(False); view._active_canvas_changed('01')
            assert not view.runs
            assert [d['bits'] for d in view.designs] == before_bits
            assert all(len(d['image_sites'])==40 for d in view.designs)
            assert '8 × 5' in view.canvas_summary.get()
            view._prepare()
            replacements, _ = parse_source2(picklist_app.SHEET_DIR/'PAINT_R1_replace.csv')
            excluded = [r for r in replacements if r['Name'] in {site_name(row,'01') for row in ROWS}]
            assert len(excluded) == 8
            base_rows, _ = parse_source1(picklist_app.SHEET_DIR/'book_base_20uM.csv', app._selected_hinge_type())
            base_lookup = {r['Replacement Well']: r['Well'] for r in base_rows}
            for run in view.runs:
                replacement_wells = {r['Source Well'] for r in run['picklist'] if r['Source Plate Name']=='SourcePlate7[7]'}
                base_wells = {r['Source Well'] for r in run['picklist'] if r['Source Plate Name']=='SourcePlate8[8]'}
                assert not replacement_wells & {r['Well'] for r in excluded}
                assert {base_lookup[r['Replace Well']] for r in excluded} <= base_wells
            settings = ReportSettings(str(path),'Stretch',25,{c:1 for c in CHANNELS},10,.5,**view._concentration_options())
            settings.validate()
            result = generate_views(settings,'tiles_1x1',0)
            assert result['geometry']['columns'] == 5
            assert result['geometry']['image_sites_per_origami'] == 40
            with patch('rgb_image_view.filedialog.askdirectory',return_value=str(tmp_path)):
                view._export()
            assert view.saved
            app._flush_config()
            error.assert_not_called()
    finally:
        app.destroy()
    reopened = picklist_app.PicklistApp()
    reopened.withdraw()
    try:
        assert reopened.rgb_image_view._active_columns() == IMAGE_COLUMNS[:-1]
        for c in IMAGE_COLUMNS[:-1]:
            reopened.rgb_image_view.active_row_vars[c].set(False)
        reopened.rgb_image_view._active_canvas_changed('02')
        assert reopened.rgb_image_view._active_columns() == ['02']
    finally:
        reopened.destroy()


@pytest.mark.parametrize('columns', [[], ['07'], ['01', '01']])
def test_invalid_canvas_rejected(columns):
    with pytest.raises(ValueError, match='analog row'):
        active_columns(columns)
