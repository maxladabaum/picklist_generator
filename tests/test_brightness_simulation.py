from copy import deepcopy
import pytest
from brightness_simulation import brightness_cases
from test_rgb_synthesis import app


def test_mapping_reproducibility_and_unchanged_synthesis():
    designs=[{'image_sites':[{'name':str(v),'replacement_fraction':v,'replacement_nl':25} for v in [0,.2,.4,.6,1]],'site_fractions':{'test':.4}}]
    before=deepcopy(designs)
    cases=brightness_cases(designs,ratio=1.2)
    assert designs==before
    assert cases==brightness_cases(designs,ratio=1.2)
    weights=lambda case: [s.get('effective_brightness',s['replacement_fraction']) for s in case[0]['image_sites']]
    assert weights(cases[1])==pytest.approx([0,1.2*.2/(1.2*.2+.8),1.2*.4/(1.2*.4+.6),1.2*.6/(1.2*.6+.4),1])
    assert weights(brightness_cases(designs,ratio=.9)[1])==pytest.approx([0,.18/.98,.36/.96,.54/.94,1])
    for case in cases:
        assert case[0]['site_fractions']==before[0]['site_fractions']
        assert weights(case)[0]==0
        assert all(s['replacement_fraction']==v and s['replacement_nl']==25 for s,v in zip(case[0]['image_sites'],[0,.2,.4,.6,1]))
    assert weights(brightness_cases(designs,cv_percent=0)[2])==[0,.2,.4,.6,1]


@pytest.mark.parametrize('kwargs',[{'cv_percent':-1},{'cv_percent':float('nan')},{'ratio':-1},{'ratio':float('inf')},{'seed':-1}])
def test_invalid_settings(kwargs):
    with pytest.raises(ValueError):brightness_cases([],**kwargs)


def test_ui_preview_is_isolated_and_invalid_input_clears_results(app):
    view=app.rgb_image_view
    view.designs=[{'image_sites':[{'name':'U-Apt_H01-01_R1','replacement_fraction':.4}]}]
    view.reconstruction=object()
    before=deepcopy(view.designs)
    view._render_overlay=lambda designs: designs
    view._draw_overlay=lambda *args: None
    view.brightness_enabled.set(True)
    view._brightness_changed()
    assert len(view.brightness_results)==4
    assert view.designs==before
    view._new_brightness_example()
    assert view.brightness_values['seed'].get()=='43'
    view.brightness_values['cv_percent'].set('-1')
    assert not view.brightness_results
    assert 'Brightness settings:' in view.brightness_summary.get()
    view.brightness_enabled.set(False)
    view._brightness_changed()
    assert not view.brightness_results


@pytest.mark.parametrize('panel_width',[225,300])
@pytest.mark.parametrize('field_size',[(81.6,99),(99,81.6)])
def test_overlay_fits_and_centers_in_its_panel(app,panel_width,field_size):
    import tkinter as tk
    from PIL import Image
    from types import SimpleNamespace
    view=app.rgb_image_view
    canvas=tk.Canvas(view,width=panel_width,highlightthickness=0)
    info=dict(display_field_width_nm=field_size[0],display_field_height_nm=field_size[1])
    view._draw_overlay(canvas,SimpleNamespace(rgb=Image.new('RGB',(100,100))),info)
    image=canvas._overlay_photo
    x,y=canvas.coords(canvas.find_all()[0])
    assert x>=6
    assert x+image.width()<=panel_width-6
    assert abs(x-(panel_width-image.width())/2)<1e-9
    assert y==7
    assert int(canvas.cget('height'))==image.height()+14
    assert abs(image.width()/image.height()-field_size[0]/field_size[1])<.01
    canvas.destroy()


def test_stock_error_shared_across_designs_and_channels():
    designs=[{'channel':channel,'image_sites':[{'name':'same_stock','replacement_fraction':p}]} for channel,p in [(0,.2),(1,.4),(2,.6)]]
    cases=brightness_cases(designs,ratio=1.2,cv_percent=20)
    def ratios(case):
        result=[]
        for d in case:
            site=d['image_sites'][0];p=site['replacement_fraction'];q=site['effective_brightness']
            result.append(q*(1-p)/(p*(1-q)))
        return result
    random=ratios(cases[2]);combined=ratios(cases[3])
    assert random==pytest.approx([random[0]]*3)
    assert combined==pytest.approx([1.2*r for r in random])
    assert cases==brightness_cases(designs,ratio=1.2,cv_percent=20)
    assert cases!=brightness_cases(designs,ratio=1.2,cv_percent=20,seed=43)
    reverse=brightness_cases(list(reversed(designs)),ratio=1.2,cv_percent=20)
    assert cases[3]==list(reversed(reverse[3]))


def test_endpoints_and_zero_variation():
    designs=[{'image_sites':[{'name':str(p),'replacement_fraction':p} for p in [0,.4,1]]}]
    for case in brightness_cases(designs,ratio=1e100,cv_percent=1000):
        sites=case[0]['image_sites']
        assert sites[0]['effective_brightness']==0
        assert sites[-1]['effective_brightness']==1
        assert all(0<=s['effective_brightness']<=1 for s in sites)
    for case in brightness_cases(designs,ratio=1,cv_percent=0):
        assert all(s['effective_brightness']==s['replacement_fraction'] for s in case[0]['image_sites'])


def test_transfer_errors_independent_reproducible_and_do_not_change_stock_draws():
    designs=[{'name':f'design_{c}', 'channel':c,'image_sites':[{'name':'same_stock','replacement_fraction':.4}]} for c in range(3)]
    def log_ratios(case):
        import math
        return [math.log(d['image_sites'][0]['effective_brightness']/(1-d['image_sites'][0]['effective_brightness']))-math.log(.4/.6) for d in case]
    cases=brightness_cases(designs,ratio=1.2,cv_percent=0,transfer_cv_percent=10)
    errors=log_ratios(cases[2])
    assert len(set(errors))==3
    assert cases==brightness_cases(designs,ratio=1.2,cv_percent=0,transfer_cv_percent=10)
    assert cases[3]==list(reversed(brightness_cases(list(reversed(designs)),ratio=1.2,cv_percent=0,transfer_cv_percent=10)[3]))
    import math
    assert log_ratios(cases[3])==pytest.approx([v+math.log(1.2) for v in errors])
    stock=log_ratios(brightness_cases(designs,cv_percent=20,transfer_cv_percent=0)[2])
    both=log_ratios(brightness_cases(designs,cv_percent=20,transfer_cv_percent=10)[2])
    assert both==pytest.approx([a+b for a,b in zip(stock,errors)])
    endpoints=[{'name':'endpoints','image_sites':[{'name':str(p),'replacement_fraction':p} for p in [0,1]]}]
    for case in brightness_cases(endpoints,transfer_cv_percent=100):
        assert [s['effective_brightness'] for s in case[0]['image_sites']]==[0,1]


@pytest.mark.parametrize('cv',[-1,float('nan'),float('inf')])
def test_invalid_transfer_cv(cv):
    with pytest.raises(ValueError):brightness_cases([],transfer_cv_percent=cv)
