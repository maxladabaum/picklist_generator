import pytest

from image_encoding import (CHANNELS, SEVEN_BIT_TILE_SCHEMES, barcode_bits,
                            compile_designs, default_barcode_schema, geometry, reconstruct)
from barcode_template_schema import from_template, to_template
from barcode_registry import from_rgb
from test_image_encoding import prepare


@pytest.mark.parametrize('key', SEVEN_BIT_TILE_SCHEMES)
def test_joint_codes_are_unique_and_seven_bits(key):
    scheme = SEVEN_BIT_TILE_SCHEMES[key]
    codes = [barcode_bits(color, x, y, key) for color in CHANNELS
             for y in range(scheme.y_states) for x in range(scheme.x_states)]
    assert all(len(bits) == 7 for bits in codes)
    assert len({tuple(bits) for bits in codes}) == scheme.design_count


def test_seven_bit_template_compile_prepare_and_registry():
    key = 'seven_tiles_6x6'
    schema = default_barcode_schema()
    schema['seven_bit'][0]['sites'] = ['H01-07', 'H03-07']
    schema, count = from_template(to_template(schema, 7))
    assert count == 7
    info = geometry(key, barcode_schema=schema)
    assert info['reserved_codes'] == list(range(108, 128))
    pixels = [[[255, 128, 0] for _ in range(info['columns'])] for _ in range(info['rows'])]
    designs = compile_designs(pixels, 50, {c: 1 for c in CHANNELS}, key, barcode_schema=schema)
    assert len(designs) == 108
    assert reconstruct(designs) == pixels
    assert {d['barcode_code'] for d in designs} == set(range(108))
    runs = prepare(designs)
    assert len(runs) == 108
    records = [from_rgb(run, 'seven-bit test') for run in runs]
    assert all(r['bit_count'] == 7 for r in records)
    assert len({r['bits'] for r in records}) == 108
    assert records[0]['schema']['seven_bit'] == schema['seven_bit']


def test_seven_bit_alignment_template_export(tmp_path):
    from alignment_template_export import export_alignment_templates
    schema = default_barcode_schema()
    info = geometry('seven_tiles_6x6', barcode_schema=schema)
    designs = compile_designs([[[0, 0, 0] for _ in range(36)] for _ in range(48)],
                              25, {c: 1 for c in CHANNELS}, 'seven_tiles_6x6')[1:3]
    snapshot = dict(batch_id='seven', designs=designs, barcode_schema=schema, geometry=info)
    runs = [dict(run_name=d['name'], destination_plate_name='Test', destination_wells=['A01']) for d in designs]
    result = export_alignment_templates(tmp_path / 'templates', snapshot, runs)
    assert result['exported_count'] == 2
    assert len(list((tmp_path / 'templates' / 'barcodes').glob('*.png'))) == 2


def test_seven_bit_capacity_stops_at_42_tiles():
    assert max(s.tile_count for s in SEVEN_BIT_TILE_SCHEMES.values()) == 42
    assert 'seven_tiles_43x1' not in SEVEN_BIT_TILE_SCHEMES
    assert 'seven_tiles_16x8' not in SEVEN_BIT_TILE_SCHEMES


def test_128_tiles_prepare_384_unique_runs():
    key = 'tiles_16x8'
    info = geometry(key)
    pixels = [[[255, 0, 255] for _ in range(info['columns'])] for _ in range(info['rows'])]
    designs = compile_designs(pixels, 25, {c: 1 for c in CHANNELS}, key)
    runs = prepare(designs)
    assert len(runs) == 384
    assert all(len(d['bits']) == 9 for d in designs)
    assert len({d['barcode_code'] for d in designs}) == 384
    assert reconstruct(designs) == pixels
    wells = [well for run in runs for well in run['destination_wells']]
    assert len(wells) == len(set(wells)) == 384
    assert len(info['reserved_codes']) == 128
