from unittest.mock import Mock, patch
import json
import pytest

from test_rgb_synthesis import app
from picklist_core import valid_transfer


@pytest.mark.parametrize('volume,base_transfer,levels',[(100,25,[0,.25,.5,.75,1]),(200,50,[n/8 for n in range(9)])])
def test_manual_targets_use_final_volume_and_independent_increment(app,volume,base_transfer,levels):
    app.base_plate.set('LowConc base plate — 20 µM (interleaved)')
    app.manual_base_target.set('5');app.manual_final_volume.set(str(volume))
    view=next(v for v in app.set_views if v.title=='LowConc PAINT R1')
    view.desired_concentration_nm.set('20')
    assert float(app.transfer_volume.get())==base_transfer
    assert list(view._fraction_options.values())==pytest.approx(levels)
    name='U-Apt_H09-03_R1'
    view.variables[name].set(True);view.site_fractions[name]=.25
    app.destination_wells.set('A01,A02,A03')
    with patch('picklist_app.messagebox.showerror') as error:
        app.generate(Mock(),Mock());error.assert_not_called()
    assert sum(row['Volume_uL'] for row in app.last_mix)==pytest.approx(volume)
    total=sum(row['Transfer Volume'] for row in app.last_picklist)/1000
    assert app.last_mix[0]['Volume_uL']==pytest.approx(total)
    # I5 is H09-03 PAINT; at 20 nM site target and 25% replacement it contributes 5 nM.
    paint=next(row for row in app.last_picklist if row['Source Plate Name']=='SourcePlate7[7]')
    assert paint['Transfer Volume']==pytest.approx(volume/4)
    assert paint['Transfer Volume']*20000/(volume*1000)==pytest.approx(5)
    assert all(valid_transfer(row['Transfer Volume'],25,25) for row in app.last_picklist)


def test_settings_sync_both_directions_and_do_not_touch_rgb(app):
    rgb={k:v.get() for k,v in app.rgb_image_view.run_variables.items()}
    app.manual_final_volume.set('200');app.manual_base_target.set('5');app.manual_scaffold_target.set('2')
    assert app.desired.get().startswith('200,5,2,')
    app.desired.set('100,10,3,1,12,5')
    assert app.manual_final_volume.get()=='100'
    assert app.manual_base_target.get()=='10'
    assert app.manual_scaffold_target.get()=='3'
    assert {k:v.get() for k,v in app.rgb_image_view.run_variables.items()}==rgb


def test_minimum_increment_invalid_targets_and_reselection(app):
    app.base_plate.set('LowConc base plate — 20 µM (interleaved)')
    app.manual_final_volume.set('200');app.manual_base_target.set('10')
    app.manual_minimum_transfer.set('50');app.manual_transfer_increment.set('25')
    view=next(v for v in app.set_views if v.title=='LowConc PAINT R1')
    view.desired_concentration_nm.set('15')
    assert float(app.transfer_volume.get())==100
    assert list(view._fraction_options.values())==pytest.approx([0,1/3,.5,2/3,1])
    name='U-Apt_H09-03_R1'
    view.variables[name].set(True);view.site_fractions[name]=1/6
    view.update_percentage_options(app.transfer_volume.get())
    assert name in view.replacement_error.get()
    with patch('picklist_app.messagebox.showerror') as error:
        app.generate(Mock(),Mock());error.assert_called_once()
    app.manual_final_volume.set('50');app.manual_base_target.set('5')
    assert app.transfer_volume.get()==''
    assert '12.5 nL' in app.manual_synthesis_status.get()
    assert not view._fraction_options
    view.variables[name].set(True);view._on_square_toggled(name)
    assert not view.variables[name].get()


def test_persist_manual_synthesis_and_custom_stock(app):
    app.manual_final_volume.set('200');app.manual_base_target.set('20')
    app.manual_minimum_transfer.set('30');app.manual_transfer_increment.set('10')
    app.staple.set('40000,1,0,0')
    app._flush_config()
    import picklist_app
    config=json.loads(picklist_app.CONFIG_PATH.read_text())
    assert config['manual_desired_mix'].startswith('200,20,')
    assert config['manual_transfer_limits']=={'minimum_transfer_nl':'30','transfer_increment_nl':'10'}
    assert config['manual_mixing_settings']['staple']=='40000,1,0,0'
    assert float(app.transfer_volume.get())==100


def test_run_settings_review_targets_without_duplicate_editors(app):
    app.manual_final_volume.set('200');app.manual_base_target.set('50');app.manual_scaffold_target.set('2')
    assert app.manual_recipe_target_summary.get()=='200 µL · book-base 50 nM · scaffold 2 nM'
    app.manual_final_buffer.set('1,15,10')
    assert app.desired.get()=='200,50,2,1,15,10'
    target_vars={str(app.desired),str(app.manual_final_volume),str(app.manual_base_target),str(app.manual_scaffold_target),
                 *(str(view.desired_concentration_nm) for view in app.set_views)}
    def children(widget):
        for child in widget.winfo_children():
            yield child
            yield from children(child)
    for widget in children(app.settings_page):
        if widget.winfo_class()=='TEntry':
            assert str(widget.cget('textvariable')) not in target_vars
    app.desired.set('100,25,3,2,12,5')
    assert app.manual_final_buffer.get()=='2,12,5'
    assert app.manual_final_volume.get()=='100'
