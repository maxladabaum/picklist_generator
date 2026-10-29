from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from picklist_core import parse_source1,parse_source2
from test_rgb_synthesis import app


@pytest.mark.parametrize('rotation', [0,90])
@pytest.mark.parametrize('base', ['Base plate — 200 µM','LowConc base plate — 20 µM (interleaved)'])
def test_inspector_functions_follow_logical_replacements_and_physical_sources(app,tmp_path,base,rotation):
    view=app.rgb_image_view
    view.auto_export_templates.set(False)
    view.optimize_enabled.set(False)
    view.alignment_enabled.set(False)
    view.barcode_schema["always_on_sites"] = ["H07-12"]
    view.base_plate.set(base)
    view.final_volume.set('200');view.base_target.set('25')
    view.tile_count.set('1');view._tile_count_changed()
    view.fit_mode.set('Stretch');view.linear_light.set(False)
    path=tmp_path/'gray.png';Image.new('RGB',(6,8),(128,128,128)).save(path)
    view.image_path.set(str(path))
    view.rotation.set(str(rotation))
    with patch('rgb_image_view.messagebox.showerror') as error:
        view._load();view._prepare();error.assert_not_called()
    base_rows,base_plate=parse_source1(Path(view.base_path.get()),'balanced_rigid' if 'interleaved' in base else 'original')
    logical={row['Well']:row['Replacement Well'] for row in base_rows}
    for run in view.runs:
        source=view.color_views[run['image_design']['color']]
        rows,_=parse_source2(Path(source.path_var.get()))
        selected=[r for r in rows if r['Name'] in run['image_design']['site_fractions']]
        targets={r['Replace Well'] for r in selected}
        assert {row['function'].split(' (')[0] for row in run['picklist']}=={'standard body','biotin','PAINT extension','body-PAINT competitor'}
        for row in run['picklist']:
            plate,well=row['Source Plate Name'],row['Source Well']
            if plate=='SourcePlate8[8]' and well in {f'P{i}' for i in range(17,25)}:
                assert row['function']=='biotin'
            elif plate==source.plate_name:
                assert row['function'].startswith('PAINT extension (')
            else:
                assert plate==base_plate
                assert row['function'].split(' (')[0]==('body-PAINT competitor' if logical[well] in targets else 'standard body')
        # All-off barcode sites still have body competitors, even with no PAINT transfer.
        off_keys={r['Replace Well'] for r in selected if run['image_design']['site_fractions'][r['Name']]==0}
        for row in run['picklist']:
            if row['Source Plate Name']==base_plate and logical.get(row['Source Well']) in off_keys:
                assert row['function'].startswith('body-PAINT competitor (')
        paint_names={r['Well']:r['Name'] for r in selected}
        competitor_names={r['Replace Well']:r['Name'] for r in selected}
        for row in run['picklist']:
            role=row['function'].split(' (')[0]
            if role not in ('PAINT extension','body-PAINT competitor'):
                continue
            name=paint_names[row['Source Well']] if role=='PAINT extension' else competitor_names[logical[row['Source Well']]]
            helix, position = name.split('_')[1].split('-')
            if role == 'PAINT extension':
                design = run['image_design']
                if [helix, position] in design['always_on_sites']:
                    kind = 'fiducial site'
                elif name in {site['name'] for site in design['image_sites']}:
                    kind = 'image site'
                else:
                    assert any([helix, position] in group for group in design['barcode_sites'])
                    kind = 'barcode site'
                assert row['function']==f'{role} ({helix}, {position}; {kind})'
            else:
                assert row['function']==f'{role} ({helix}, {position})'
        view.design_name.set(run['run_name']);view._draw_design()
        assert view.picklist_tree['columns'][0]=='function'
        displayed=[view.picklist_tree.item(item,'values')[0] for item in view.picklist_tree.get_children()]
        assert displayed==[row['function'] for row in run['picklist']]
        assert view.site_tree['columns'][:2]==('Helix','Row')
        displayed_sites=[view.site_tree.item(item,'values')[:2] for item in view.site_tree.get_children()]
        assert displayed_sites==[(site['row'],site['column']) for site in run['image_design']['image_sites']]
        # Row selection locates the physical site at either display rotation.
        for tab, tree in ((0, view.site_tree), (1, view.picklist_tree)):
            view.design_inspector.select(tab)
            target = next(item for item in tree.get_children()
                          if (tree.item(item, 'values')[:2] == ('H09', '03') if tab == 0
                              else tree.item(item, 'values')[0].startswith('PAINT extension (H09, 03;')))
            tree.selection_set(target)
            tree.event_generate("<<TreeviewSelect>>", when="now")
            view._highlight_selected_site()
            markers = view.design_canvas.find_withtag('selected_site')
            assert len(markers) == 2
            site = view.design_canvas.find_withtag('site_H09_03')[0]
            x1,y1,x2,y2 = view.design_canvas.coords(site)
            assert view.design_canvas.coords(markers[-1]) == [x1-5,y1-5,x2+5,y2+5]
        for role in ('standard body', 'biotin'):
            item = next(item for item in view.picklist_tree.get_children()
                        if view.picklist_tree.item(item, 'values')[0] == role
                        and view.picklist_site_locations[item])
            view.picklist_tree.selection_set(item)
            view._highlight_selected_site()
            helix, position = view.picklist_site_locations[item]
            source_site = view.design_canvas.find_withtag(f'site_{helix}_{position}')[0]
            x1,y1,x2,y2 = view.design_canvas.coords(source_site)
            assert view.design_canvas.coords(view.design_canvas.find_withtag('selected_site')[-1]) == [x1-5,y1-5,x2+5,y2+5]
        # An unmapped transfer explicitly reports that its location is unavailable.
        body = next(item for item in view.picklist_tree.get_children()
                    if view.picklist_tree.item(item, 'values')[0] == 'standard body'
                    and not view.picklist_site_locations[item])
        view.picklist_tree.selection_set(body)
        view._highlight_selected_site()
        marker = view.design_canvas.find_withtag('selected_site')
        assert len(marker) == 1
        assert view.design_canvas.itemcget(marker[0], 'text') == 'Location unavailable for this staple'
        view.design_inspector.select(0)
        view._highlight_selected_site()
        assert view.design_canvas.find_withtag('selected_site')
        view.design_inspector.select(2)
        view._highlight_selected_site()
        assert not view.design_canvas.find_withtag('selected_site')
        view._draw_design()
        view.design_inspector.select(0)
        view._highlight_selected_site()
        assert not view.design_canvas.find_withtag('selected_site')
    assert all(any(f'; {kind})' in row['function'] for run in view.runs for row in run['picklist'])
               for kind in ('image site', 'barcode site', 'fiducial site'))
