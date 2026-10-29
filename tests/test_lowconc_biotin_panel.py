from unittest.mock import Mock, patch

from test_rgb_synthesis import app


def test_lowconc_biotin_panel_routes_to_base_stock_and_checks_clashes(app):
    view = next(v for v in app.set_views if v.title == 'LowConc PAINT R1')
    biotin_names = {name for name in view.variables if name.startswith('D-Biotin_')}
    assert len(biotin_names) == 8
    assert all(view.selectable[name] for name in biotin_names)
    assert view.sheet_path_entry.get().endswith('/PAINT_R1_replace.csv')
    assert view.source_plate_label.get() == 'SourcePlate7[7]'
    view.panel_var.set('D-Biotin'); view._show_panel()
    assert view.sheet_path_entry.get().endswith('/lowconc_D_biotin_additions.csv')
    assert view.source_plate_label.get() == 'SourcePlate8[8]'
    assert str(view.sheet_path_entry['state']) == 'readonly'
    view.panel_var.set('U-Apt R1'); view._show_panel()
    assert view.sheet_path_entry.get().endswith('/PAINT_R1_replace.csv')
    assert view.source_plate_label.get() == 'SourcePlate7[7]'
    view.panel_var.set('D-Biotin'); view._show_panel()
    for name in biotin_names:
        view.variables[name].set(True)
    paint_name = 'U-Apt_H09-03_R1'
    view.variables[paint_name].set(True)
    view.site_concentrations_nm['D-Biotin_H01-01'] = 100
    app.desired.set('100,50,10,1,12,5')
    app.destination_wells.set('A01')
    with patch('picklist_app.messagebox.showerror') as error:
        app.generate(Mock(), Mock())
        error.assert_not_called()
    biotin = [row for row in app.last_picklist if row['Source Plate Name'] == 'SourcePlate8[8]']
    assert {row['Source Well'] for row in biotin} == {f'P{i}' for i in range(17,25)}
    assert {row['Source Well']: row['Transfer Volume'] for row in biotin} == {
        f'P{i}': (500 if i == 17 else 125) for i in range(17,25)}
    assert any(row['Source Plate Name'] == 'SourcePlate7[7]' for row in app.last_picklist)
    assert not any(row['Source Plate Name'] == 'SourcePlate5[5]' for row in app.last_picklist)
    assert any(panel['label'] == 'D-Biotin' for panel in app.last_selection_panels)
    p1 = next(v for v in app.set_views if v.title == 'PAINT P1')
    p1.variables['D-Biotin_H01-01'].set(True)
    assert not app.check_selections()
    assert 'D-Biotin_H01-01' in view.conflicts
    # RGB color tabs retain their original U-Apt panel only.
    assert all(len(v.panels) == 1 for v in app.set_views if v.title in ('R1 R','R1 G','R1 B'))
