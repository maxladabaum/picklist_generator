from unittest.mock import patch

import pytest
from PIL import Image

from image_encoding import rgb_replacement_options, rgb_transfer_grid, synthesis_base_volume_nl
from test_rgb_synthesis import app


@pytest.mark.parametrize('target,expected',[(5,[0,1]),(10,[0,.5,1]),(25,[0,.2,.4,.6,.8,1])])
def test_levels_from_final_volume_and_low_concentration_stocks(target,expected):
    result=rgb_replacement_options(100,target,20000,20000)
    assert result['fractions']==pytest.approx(expected)
    assert result['full_replacement_nl']==target*5
    for f in result['fractions']:
        assert result['full_replacement_nl']*f/25==pytest.approx(round(result['full_replacement_nl']*f/25))
        assert result['full_original_nl']*(1-f)/25==pytest.approx(round(result['full_original_nl']*(1-f)/25))


def test_concentrated_original_stock_limits_low_targets():
    result=rgb_replacement_options(100,5,200000,20000)
    assert result['fractions']==[1]
    assert result['full_original_nl']==2.5
    assert result['full_replacement_nl']==25


@pytest.mark.parametrize('volume,target,base_stock,rgb_stock',[(100,5,20000,20000),(200,50,200000,20000),(100,50,20000,20000)])
def test_displayed_levels_match_encoding_grid(volume,target,base_stock,rgb_stock):
    result=rgb_replacement_options(volume,target,base_stock,rgb_stock)
    base_target=50
    base_volume=synthesis_base_volume_nl(volume,base_target,base_stock)
    grid=rgb_transfer_grid(base_volume,target,base_target,base_stock,rgb_stock)
    assert result['fractions']==pytest.approx([i/grid['fraction_steps'] for i in range(grid['fraction_steps']+1)])


def test_live_levels_and_image_quantization_follow_inputs(app,tmp_path):
    view=app.rgb_image_view
    view.base_plate.set('LowConc base plate — 20 µM (original layout)')
    view.base_target.set('25')
    view.final_volume.set('100')
    for source in view.color_views.values():
        source.desired_concentration_nm.set('5')
    assert 'Available: 0%, 100% (2 levels)' in view.replacement_options['R1 R'].get()
    assert '100% PAINT: 25 nL' in view.replacement_options['R1 R'].get()
    view.tile_count.set('1');view._tile_count_changed()
    view.fit_mode.set('Stretch');view.linear_light.set(False)
    path=tmp_path/'gray.png';Image.new('RGB',(6,8),(128,128,128)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load();error.assert_not_called()
    assert all(d['fraction_steps']==1 for d in view.designs)
    view.color_views['R1 R'].desired_concentration_nm.set('10')
    assert 'Available: 0%, 50%, 100% (3 levels)' in view.replacement_options['R1 R'].get()
    red=next(d for d in view.designs if d['color']=='R1 R')
    assert red['fraction_steps']==2
    assert all(site['replacement_fraction']==.5 for site in red['image_sites'])
    assert all(d['fraction_steps']==1 for d in view.designs if d['color']!='R1 R')
    view.final_volume.set('200')
    assert 'Available: 0%, 25%, 50%, 75%, 100%' in view.replacement_options['R1 R'].get()
    before=view.replacement_options['R1 R'].get()
    view.base_target.set('50')
    assert view.replacement_options['R1 R'].get()==before
    view.base_plate.set('Base plate — 200 µM')
    assert 'RGB unavailable' in view.replacement_options['R1 R'].get()
    view.color_views['R1 R'].desired_concentration_nm.set('')
    assert 'No available levels' in view.replacement_options['R1 R'].get()
