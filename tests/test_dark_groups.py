import copy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from picklist_app import OrigamiTemplateView
from template_generator import normalize_logical_bit_schema, logical_template_sites, render_barcode_template


def schema():
    return dict(format='paint-analysis-logical-bits-v1', physical_rows=3, physical_columns=3,
                alignment_groups=[dict(id='fid', physical_sites=[[1, 1], [3, 3]])],
                logical_bits=[], dark_groups=[dict(id='empty', physical_sites=[[2, 2]], mask_radius_nm=3.)])


def test_dark_groups_export_as_metadata_without_bright_signal_or_barcode_bits():
    model = normalize_logical_bit_schema(schema(), 3, 3)
    assert logical_template_sites(3, 3, [], model) == [(0, 0), (2, 2)]
    # Even a caller that supplies the dark site as selected cannot light it up.
    *_, metadata = render_barcode_template(3, 3, [(0, 0), (1, 1), (2, 2)], logical_schema=model)
    assert metadata['logical_model']['dark_groups'][0]['mask_radius_nm'] == 3.
    assert metadata['logical_model']['active_logical_bits'] == []
    assert {(site['row'], site['column']) for site in metadata['selected_sites']} == {(1, 1), (3, 3)}
    restored = normalize_logical_bit_schema(metadata['logical_model'], 3, 3)
    assert restored['dark_groups'] == model['dark_groups']


@pytest.mark.parametrize('radius', [0, -1, float('nan'), float('inf'), True, '3'])
def test_invalid_dark_radius_is_rejected(radius):
    model = schema()
    model['dark_groups'][0]['mask_radius_nm'] = radius
    with pytest.raises(ValueError, match='mask_radius_nm'):
        normalize_logical_bit_schema(model, 3, 3)


def test_dark_and_bright_sites_cannot_conflict():
    model = schema()
    model['dark_groups'][0]['physical_sites'] = [[1, 1]]
    with pytest.raises(ValueError, match='share physical sites'):
        normalize_logical_bit_schema(model, 3, 3)


def test_editor_creates_and_updates_dark_groups():
    var = lambda value: SimpleNamespace(get=lambda: value)
    app = SimpleNamespace(selected_group_ids=[], selected_group_id=None,
        group_name_var=var('empty'), group_role_var=var('Dark (alignment)'),
        group_active_var=var(True), group_brightness_var=var('invalid unused brightness'),
        group_dark_radius_var=var('3'), logical_schema=None, _built_shape=(3, 3),
        _selected_sites=lambda: {(1, 1)}, _group_by_id=lambda _: None,
        _active_bit_ids=lambda: [], _all_groups=lambda: [], _unique_group_id=lambda *_: 'empty',
        _build_logical_controls=Mock(), _settings_changed=Mock())
    OrigamiTemplateView._store_group(app, create=True)
    assert app.logical_schema['dark_groups'][0]['mask_radius_nm'] == 3.
    assert app.logical_schema['active_logical_bits'] == []
    app.group_dark_radius_var = var('4')
    OrigamiTemplateView._store_group(app, create=False)
    assert len(app.logical_schema['dark_groups']) == 1
    assert app.logical_schema['dark_groups'][0]['mask_radius_nm'] == 4.
    before = copy.deepcopy(app.logical_schema)
    app.group_dark_radius_var = var('-1')
    with patch('picklist_app.messagebox.showerror') as error:
        OrigamiTemplateView._store_group(app, create=False)
        error.assert_called_once()
    assert app.logical_schema == before
