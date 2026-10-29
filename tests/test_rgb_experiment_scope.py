import pytest
from barcode_registry import BarcodeRegistry, from_rgb
from image_encoding import CHANNELS, compile_designs, default_barcode_schema
from destination_assignment import assign_destination_wells


def run(identity, schema=None):
    design = compile_designs([[[0, 0, 0] for _ in range(6)] for _ in range(8)],
                             25, {c: 1 for c in CHANNELS}, 'seven_tiles_1x1', barcode_schema=schema)[1]
    return dict(id=identity, run_name=identity, image_design=design,
                destination_plate_name='Destination[1]', destination_wells=['E01'],
                picklist=[{'Destination Well': 'E01'}])


def test_new_pool_allows_different_layout_without_erasing_old_registry(tmp_path):
    registry = BarcodeRegistry(tmp_path / 'registry.json')
    registry.reserve([from_rgb(run('old'), 'Default')])
    schema = default_barcode_schema()
    schema['seven_bit'][0]['sites'] = ['H01-07', 'H03-07']
    candidate = run('new', schema)
    with pytest.raises(ValueError, match='new Experiment / pool') as error:
        registry.check(from_rgb(candidate, 'Default'))
    assert 'E01' in str(error.value)
    assert "'Default'" in str(error.value)
    registry.check(from_rgb(candidate, 'Big Mona Lisa'))
    assert len(registry.entries) == 1
    assert not registry.entries[0]['released']


def test_preflight_records_are_refreshed_after_manual_well_assignment():
    candidate = run('new')
    candidate['barcode_record'] = from_rgb(candidate, 'New pool')
    remapped = assign_destination_wells([candidate], ['P24'])[0]
    refreshed = from_rgb(remapped, 'New pool')
    assert refreshed['destination_wells'] == ['P24']
    assert refreshed['id'] == candidate['barcode_record']['id']
    assert refreshed['bits'] == candidate['barcode_record']['bits']
