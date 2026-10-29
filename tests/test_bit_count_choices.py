import pytest
from image_encoding import schemes_for_bits, direct_tile_schemes, barcode_bits


@pytest.mark.parametrize('count,limit', [(6, 21), (7, 42), (9, 128)])
def test_choices_match_explicit_bit_count_and_capacity(count, limit):
    choices = schemes_for_bits(count)
    assert choices
    assert all(s.bit_count == count for s in choices.values())
    tiles = direct_tile_schemes(count)
    assert {s.tile_count for s in tiles.values()} == set(range(1, limit + 1))
    for key, scheme in tiles.items():
        codes = [tuple(barcode_bits(color, x, y, key))
                 for color in ('R1 R', 'R1 G', 'R1 B')
                 for y in range(scheme.y_states) for x in range(scheme.x_states)]
        assert all(len(bits) == count for bits in codes)
        assert len(set(codes)) == scheme.tile_count * 3


def test_seven_bit_choices_exclude_nine_bit_schemes():
    choices = schemes_for_bits(7)
    assert 'seven_tiles_6x6' in choices
    assert 'nine_joint_18x9' not in choices
    assert 'nine_fixed_16x8' not in choices
    assert 'tiles_16x8' not in choices
