import csv
from copy import deepcopy
from unittest.mock import patch

from PIL import Image

from test_rgb_synthesis import app


def prepare(view, tmp_path):
    view.auto_export_templates.set(False)
    view.tile_count.set('1'); view._tile_count_changed()
    path=tmp_path/'image.png'
    Image.new('RGB',(6,8),(255,128,0)).save(path)
    view.image_path.set(str(path))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load(); view._prepare(); error.assert_not_called()


def test_exports_exact_prepared_recipes_without_committing_plate(app,tmp_path):
    view=app.rgb_image_view
    assert str(view.recipe_export_button['state'])=='disabled'
    prepare(view,tmp_path)
    assert str(view.recipe_export_button['state'])=='normal'
    state=deepcopy(app.plate_state)
    runs=deepcopy(view.runs)
    destination=tmp_path/'recipes'
    with patch('rgb_image_view.filedialog.askdirectory',return_value=str(destination)), \
         patch('rgb_image_view.messagebox.showerror') as error:
        view.recipe_export_button.invoke()
        error.assert_not_called()
    batch=next(destination.glob('*/mixing_recipe_batch.csv'))
    with batch.open() as handle:
        summary=list(csv.DictReader(handle))
    assert len(summary)==sum(len(run['mixing_recipe']) for run in runs)
    for run in runs:
        with (batch.parent/run['run_name']/'mixing_recipe.csv').open() as handle:
            rows=list(csv.DictReader(handle))
        assert [(r['Reagent'],float(r['Volume_uL'])) for r in rows] == [(r['Reagent'],r['Volume_uL']) for r in run['mixing_recipe']]
        selected=[r for r in summary if r['Run']==run['run_name']]
        assert all(r['Destination Plate']==run['destination_plate_name'] for r in selected)
        assert all(r['Destination Wells']==', '.join(run['destination_wells']) for r in selected)
    assert not list(batch.parent.rglob('picklist*.csv'))
    assert app.plate_state==state
    assert view.runs==runs
    assert not view.committed and not view.saved and not view.stored
    # Saving an image batch must not prevent independent recipe export.
    view.saved=True
    with patch('rgb_image_view.filedialog.askdirectory',return_value=str(destination)), \
         patch('rgb_image_view.messagebox.showerror') as error:
        view._export_recipes(); error.assert_not_called()
    assert len(list(destination.glob('*/mixing_recipe_batch.csv')))==2
    view.biotin_target.set('100')
    assert str(view.recipe_export_button['state'])=='disabled'


def test_cancel_and_write_failure_leave_runs_available(app,tmp_path):
    view=app.rgb_image_view
    prepare(view,tmp_path)
    runs=deepcopy(view.runs)
    with patch('rgb_image_view.filedialog.askdirectory',return_value=''), \
         patch('rgb_image_view.write_csv') as write:
        view._export_recipes(); write.assert_not_called()
    with patch('rgb_image_view.filedialog.askdirectory',return_value=str(tmp_path/'failed')), \
         patch('rgb_image_view.write_csv',side_effect=OSError('Disk full')), \
         patch('rgb_image_view.messagebox.showerror') as error:
        view._export_recipes()
        assert error.call_args.args[0]=='Cannot save mixing recipe batch'
    assert view.runs==runs
    assert not view.committed
    assert str(view.recipe_export_button['state'])=='normal'
