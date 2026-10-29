"""Bridge the visual Origami Templates editor and RGB barcode site schemas."""
from image_encoding import BARCODE_SCHEMA_KEYS
import re

from image_encoding import ROWS, default_barcode_schema, validate_barcode_schema
from template_generator import empty_logical_bit_schema, normalize_logical_bit_schema


def from_template(payload, current=None):
    if not isinstance(payload, dict):
        raise ValueError('Schema must contain a JSON object.')
    raw = payload.get('logical_model', payload)
    model = normalize_logical_bit_schema(raw, 8, 12)
    groups = model['logical_bits']
    count = len(groups)
    if count not in (6, 7, 9):
        raise ValueError('RGB needs exactly 6, 7, or 9 Digital bit groups on an 8-row × 12-column template.')
    # Named bits remain stable when the editor removes and re-appends an edited
    # group. Unnumbered legacy schemas retain their explicit logical_bits order.
    numbers = []
    for group in groups:
        match = re.fullmatch(r'bit[\s_-]*([1-9])', group['label'], re.IGNORECASE)
        numbers.append(int(match[1]) if match else None)
    if any(n is not None for n in numbers):
        if set(numbers) != set(range(1, count+1)):
            raise ValueError('Name every digital group Bit 1 through Bit {} exactly once.'.format(count))
    else:
        numbers = list(range(1, count+1))

    def sites(group):
        # Template C1 is physical column 01, even though the editor mirrors it
        # to the right of C12. R1..R8 correspond to H01,H03,...,H15.
        return ['{}-{:02d}'.format(ROWS[row-1], column) for row, column in group['physical_sites']]

    result = validate_barcode_schema(current) if current is not None else default_barcode_schema()
    result['name'] = 'Origami Templates — {} bits'.format(count)
    result[BARCODE_SCHEMA_KEYS[count]] = sorted(
        [{'bit': number, 'sites': sites(group)} for number, group in zip(numbers, groups)], key=lambda bit: bit['bit'])
    result['always_on_sites'] = list(dict.fromkeys(site for group in model['alignment_groups'] for site in sites(group)))
    return validate_barcode_schema(result, count), count


def to_template(schema, bit_count):
    value = validate_barcode_schema(schema, bit_count)
    result = empty_logical_bit_schema(8, 12)
    result.update(spacing_x_nm=9.9, spacing_y_nm=5.1, column_offsets_nm=[0.0]*6+[10.0]*6)

    def coordinates(sites):
        return [[ROWS.index(site.split('-')[0])+1, int(site.split('-')[1])] for site in sites]

    result['logical_bits'] = [dict(id='bit_{}'.format(bit['bit']), label='Bit {}'.format(bit['bit']),
        physical_sites=coordinates(bit['sites'])) for bit in value[BARCODE_SCHEMA_KEYS[bit_count]]]
    if value['always_on_sites']:
        result['alignment_groups'] = [dict(id='fiducials', label='Permanent fiducials',
            physical_sites=coordinates(value['always_on_sites']))]
    result = normalize_logical_bit_schema(result, 8, 12)
    result['active_logical_bits'] = [g['id'] for g in result['logical_bits']]
    return result
