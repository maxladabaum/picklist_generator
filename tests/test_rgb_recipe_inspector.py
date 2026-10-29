from collections import defaultdict
from unittest.mock import patch

import pytest
from PIL import Image

from test_rgb_synthesis import app


def test_prepared_recipe_shows_needed_staples_and_actual_volume_in_each_well(app,tmp_path):
    view=app.rgb_image_view
    assert not view.recipe_tree.get_children()
    assert not view.recipe_well_tree.get_children()
    view.auto_export_templates.set(False)
    view.max_volume.set('1')
    view.tile_count.set('1');view._tile_count_changed()
    view.fit_mode.set('Stretch')
    path=tmp_path/'image.png';Image.new('RGB',(6,8),(255,128,0)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load();view._prepare();error.assert_not_called()
    for run in (view.runs[0],view.runs[-1]):
        view.design_name.set(run['run_name']);view._draw_design()
        expected=defaultdict(float)
        for row in run['picklist']:
            expected[row['Destination Plate Name'],row['Destination Well']]+=row['Transfer Volume']/1000
        assert len(expected)>1
        displayed={}
        for item in view.recipe_well_tree.get_children():
            plate,well,volume=view.recipe_well_tree.item(item,'values')
            displayed[plate,well]=float(volume)
        assert displayed==pytest.approx(dict(expected))
        assert any(volume<1 for volume in displayed.values())
        recipe=[view.recipe_tree.item(item,'values') for item in view.recipe_tree.get_children()]
        assert recipe[0][0]=='Staple mix required (pooled)'
        assert float(recipe[0][1])==pytest.approx(run['mixing_recipe'][0]['Volume_uL'])
        assert float(recipe[0][1])==pytest.approx(sum(displayed.values()))
        assert [row[0] for row in recipe[1:]]==[row['Reagent'] for row in run['mixing_recipe'][1:]]
        assert [float(row[1]) for row in recipe]==pytest.approx([row['Volume_uL'] for row in run['mixing_recipe']],rel=1e-5)
        assert run['run_name'] in view.recipe_summary.get()
        assert 'Final synthesis: 100 µL' in view.recipe_summary.get()
    view.biotin_target.set('100')
    assert not view.recipe_tree.get_children()
    assert not view.recipe_well_tree.get_children()
    assert 'Prepare runs' in view.recipe_summary.get()
