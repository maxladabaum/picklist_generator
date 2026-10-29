import pytest

from image_encoding import rgb_replacement_options, rgb_transfer_grid, synthesis_base_volume_nl


def test_50ul_valid_base_but_rgb_target_falls_between_transfer_increments():
    base = synthesis_base_volume_nl(50, 10, 20000)
    assert base == 25
    for calculate in (lambda: rgb_replacement_options(50, 25, 20000, 20000),
                      lambda: rgb_transfer_grid(base, 25, 10, 20000, 20000)):
        with pytest.raises(ValueError, match='62.5 nL') as error:
            calculate()
        assert 'at least 25 nL AND a multiple of 25 nL' in str(error.value)
    # Valid alternatives retain the configured minimum and increment.
    assert rgb_replacement_options(100, 25, 20000, 20000)['full_replacement_nl'] == 125
    assert rgb_replacement_options(50, 20, 20000, 20000)['full_replacement_nl'] == 50
    assert rgb_replacement_options(50, 30, 20000, 20000)['full_replacement_nl'] == 75
