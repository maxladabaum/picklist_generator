"""Tile counts drive complete RGB batches and preserve unique color barcodes."""
from unittest.mock import patch

import pytest
from PIL import Image

from image_encoding import (CHANNELS, COLOR_BITS, TILE_COUNT_SCHEMES,
                            compile_designs, geometry, reconstruct)


@pytest.mark.parametrize('key', TILE_COUNT_SCHEMES)
def test_tile_count_codebook_and_image_coverage(key):
    scheme = TILE_COUNT_SCHEMES[key]
    info = geometry(key)
    pixels = [[[255, 128, 0] for _ in range(info['columns'])] for _ in range(info['rows'])]
    designs = compile_designs(pixels, 50, {c: 1 for c in CHANNELS}, key)
    assert len(designs) == 3 * scheme.tile_count
    assert len({d['barcode_code'] for d in designs}) == len(designs)
    seen = set()
    for design in designs:
        assert len(design['bits']) == (6 if scheme.tile_count <= 16 else 9)
        assert design['bits'][:2] == list(COLOR_BITS[design['color']])
        for site in design['image_sites']:
            identity = (design['channel'], site['pixel_x'], site['pixel_y'])
            assert identity not in seen
            seen.add(identity)
    assert len(seen) == info['columns'] * info['rows'] * 3
    assert reconstruct(designs) == pixels


def test_six_by_six_capacity_and_barcode_metadata():
    scheme = TILE_COUNT_SCHEMES['tiles_6x6']
    info = geometry('tiles_6x6')
    assert scheme.tile_count == 36
    assert scheme.design_count == 108
    assert scheme.bit_count == 9
    assert max(s.tile_count for s in TILE_COUNT_SCHEMES.values()) == 128
    assert info['code_formula'] == 'color_code * 128 + y_shift * x_states + x_shift'


def test_controls_rebuild_prepare_invalidate_and_restore(tmp_path, monkeypatch):
    import picklist_app
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + '.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        # Use two original-stock droplets for this workflow check.
        app.rgb_image_view.final_volume.set('200')
        view = app.rgb_image_view
        view.tile_count.set('4')
        view._tile_count_changed()
        assert view._scheme_id() == 'tiles_2x2'
        path = tmp_path / 'white.png'
        Image.new('RGB', (24, 32), 'white').save(path)
        with patch('rgb_image_view.messagebox.showerror') as error:
            view.image_path.set(str(path))
            view._load()
            assert len(view.designs) == 12
            view._prepare()
            assert len(view.runs) == 12
            view.barcode_bit_count.set('7')
            view._bit_count_changed()
            view.tile_count.set('36')
            view._tile_count_changed()
            assert view._scheme_id() == 'seven_tiles_6x6'
            assert len(view.designs) == 108
            assert not view.runs
            view._prepare()
            assert len(view.runs) == 108
            view.barcode_bit_count.set('9')
            view._bit_count_changed()
            view.tile_count.set('128')
            view._tile_count_changed()
            assert view._scheme_id() == 'tiles_16x8'
            assert len(view.designs) == 384
            assert not view.runs
            view.barcode_bit_count.set('6')
            view._bit_count_changed()
            view.tile_count.set('3')
            view._tile_count_changed()
            assert len(view.designs) == 9
            assert not view.runs
            view.tile_layout.set('1 × 3')
            view._tile_layout_changed()
            assert view._scheme_id() == 'tiles_1x3'
            assert len(view.designs) == 9
            assert app.config['image_encoding_scheme'] == 'tiles_1x3'
            # A saved scheme or comparison selection synchronizes both controls.
            view.tile_count.set('16')
            view._sync_tile_controls()
            assert view.tile_count.get() == '3'
            error.assert_not_called()
    finally:
        app.destroy()


@pytest.mark.parametrize('saved_scheme, count, layout, runs', [
    ('sweep_fixed_4x4_tiles_4x4', '16', '2 × 8', 48),
    ('tiles_6x6', '36', '4 × 9', 108),
    ('tiles_16x8', '128', '8 × 16', 384),
    ('tiles_5x5', '25', '5 × 5', 75),
    ('quadrants_2x2', 'Use encoding scheme', '1 × 4', 48),
])
def test_saved_layout_grid_dropdown_events(tmp_path, monkeypatch, saved_scheme, count, layout, runs):
    import json
    import picklist_app
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + '.json'))
    picklist_app.CONFIG_PATH.write_text(json.dumps({'image_encoding_scheme': saved_scheme}))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        view = app.rgb_image_view
        assert view.tile_count.get() == count
        assert str(view.tile_layout_picker['state']) == 'readonly'
        choices = tuple(view.tile_layout_picker['values'])
        assert layout in choices
        view.runs = [{'previous': True}]
        view.tile_layout_picker.current(choices.index(layout))
        view.tile_layout_picker.event_generate('<<ComboboxSelected>>')
        assert view.tile_layout.get() == layout
        assert view.image_geometry['design_count'] == runs
        assert (view.image_geometry['tile_columns'], view.image_geometry['tile_rows']) == tuple(map(int, layout.split(' × ')))
        assert not view.runs
        assert app.config['image_encoding_scheme'] == view._scheme_id()
    finally:
        app.destroy()


def test_tile_boundaries_survive_invalid_transfers_and_rotation(tmp_path, monkeypatch):
    import picklist_app
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + '.json'))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        view = app.rgb_image_view
        view.tile_count.set('16')
        view._tile_count_changed()
        path = tmp_path / 'white.png'
        Image.new('RGB', (24, 32), 'white').save(path)
        view.image_path.set(str(path))
        view.color_views['R1 R'].desired_concentration_nm.set('0')
        with patch('rgb_image_view.messagebox.showerror') as error:
            for rotation in (0, 90, 180, 270):
                view.rotation.set(str(rotation))
                view._load()
                assert not view.designs  # A zero plate target remains invalid.
                canvas = view.pixel_canvases[0]
                tiles = canvas.find_withtag('tile_grid')
                assert len(tiles) == 32  # Black halo and colored outline per tile.
                bounds = [canvas.coords(item) for item in tiles[1::2]]
                assert len({tuple(b) for b in bounds}) == 16
                assert all(b[2] > b[0] and b[3] > b[1] for b in bounds)
                assert view.pixel_canvases[1].find_withtag('preview_error')
            view.color_views['R1 R'].desired_concentration_nm.set('50')
            assert len(view.designs) == 48
            assert len(view.pixel_canvases[0].find_withtag('tile_grid')) == 32
            assert view.pixel_canvases[0].find_withtag('design_pixels')
            assert not view.pixel_canvases[1].find_withtag('preview_error')
            error.assert_not_called()
    finally:
        app.destroy()
