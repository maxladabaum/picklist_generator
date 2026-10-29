from image_encoding import FIT_MODES, ranked_schemes, usable_pixel_score


def test_square_grid_beats_strip_for_same_tile_budget():
    keys = ['tiles_16x1', 'tiles_4x4']
    assert ranked_schemes(keys, (100, 100)) == keys[::-1]
    assert usable_pixel_score('tiles_4x4') > 10 * usable_pixel_score('tiles_16x1')


def test_panorama_favors_strip_and_orientation_is_automatic():
    keys = ['tiles_4x4', 'tiles_16x1']
    assert ranked_schemes(keys, (2400, 100)) == keys[::-1]
    assert ranked_schemes(keys, (100, 2400)) == keys[::-1]


def test_crop_and_stretch_use_entire_sampling_grid():
    for mode in FIT_MODES[1:]:
        assert usable_pixel_score('tiles_16x1', fit_mode=mode) == 16 * 48
        assert usable_pixel_score('tiles_4x4', fit_mode=mode) == 16 * 48
        assert ranked_schemes(['tiles_1x1', 'tiles_4x4'], fit_mode=mode) == ['tiles_4x4', 'tiles_1x1']


def test_encoding_family_does_not_change_usable_pixels():
    assert usable_pixel_score('seven_tiles_6x6') == usable_pixel_score('tiles_6x6')
    assert usable_pixel_score('tiles_4x4', active_image_columns=['06', '05']) < usable_pixel_score('tiles_4x4')


def test_pixel_summary_counts_rgb_locations_once():
    from image_encoding import scheme_pixel_summary
    assert scheme_pixel_summary('seven_tiles_6x6', fit_mode='Stretch') == (
        '≈1,728 usable / 1,728 total RGB pixels (36×48) · 108 runs')
    assert '768 total RGB pixels' in scheme_pixel_summary('tiles_4x4')
    assert '768 total RGB pixels' in scheme_pixel_summary('tiles_16x1')
    assert '≈528 usable' in scheme_pixel_summary('tiles_4x4')
    assert '≈33 usable' in scheme_pixel_summary('tiles_16x1')
    assert '864 total RGB pixels (18×48)' in scheme_pixel_summary(
        'seven_tiles_6x6', active_image_columns=['06', '05', '04'])
