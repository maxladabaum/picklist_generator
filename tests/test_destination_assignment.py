"""Manual destination assignment preserves per-run pooling and transfer contents."""
from copy import deepcopy

import pytest

from destination_assignment import (assign_destination_wells, assignment_slots,
                                    automatic_destination_wells)
from test_image_encoding import designs, prepare


def test_manual_assignment_preserves_real_transfers_and_recipes():
    runs = prepare(designs()[:2], max_destination_volume_ul=1)
    before = deepcopy(runs)
    assert len(runs[0]['destination_wells']) > 1
    count = len(assignment_slots(runs))
    from app_state import all_384_wells
    selected = list(reversed(all_384_wells()))[:count]
    assigned = assign_destination_wells(runs, selected, used={'A01'})
    assert runs == before
    assert [well for run in assigned for well in run['destination_wells']] == selected
    for original, updated in zip(runs, assigned):
        assert updated['mixing_recipe'] == original['mixing_recipe']
        mapping = dict(zip(original['destination_wells'], updated['destination_wells']))
        assert updated['picklist'] == [dict(row, **{'Destination Well': mapping[row['Destination Well']]})
                                       for row in original['picklist']]


@pytest.mark.parametrize('wells,used,message', [
    ([], [], 'exactly'),
    (['A01', 'A01'], [], 'only once'),
    (['A01', 'Q01'], [], 'valid destination'),
    (['A01', 'P24'], ['P24'], 'occupied'),
])
def test_invalid_manual_assignments_are_rejected_without_mutation(wells, used, message):
    runs = [{'run_name': 'test', 'destination_wells': ['A01', 'A02'], 'picklist': []}]
    original = deepcopy(runs)
    with pytest.raises(ValueError, match=message):
        assign_destination_wells(runs, wells, used)
    assert runs == original


def test_automatic_assignment_respects_start_and_occupied_wells():
    assert automatic_destination_wells(3, {'B01', 'B03'}, 'b1') == ['B02', 'B04', 'B05']
    with pytest.raises(ValueError, match='Not enough'):
        automatic_destination_wells(2, set(), 'P24')
    with pytest.raises(ValueError, match='starting well'):
        automatic_destination_wells(1, set(), 'invalid')


@pytest.mark.parametrize('action', ['manual', 'automatic', 'cancel'])
def test_plate_picker_dialog(action):
    tk = pytest.importorskip('tkinter')
    from tkinter import ttk
    from destination_assignment import choose_destination_wells
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(str(exc))
    runs = [{'run_name': 'red', 'destination_wells': ['A01', 'A02']}]
    errors = []

    def interact():
        window = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        try:
            widgets = list(descendants(window))
            buttons = {w.cget('text'): w for w in widgets if isinstance(w, ttk.Button)}
            if action == 'cancel':
                buttons['Cancel'].invoke()
            elif action == 'automatic':
                buttons['Assign automatically'].invoke()
            else:
                buttons['Assign manually'].invoke()
                root.update()
                canvas = next(w for w in widgets if isinstance(w, tk.Canvas))
                def click(well):
                    x1, y1, x2, y2 = canvas.bbox(canvas.find_withtag(well)[0])
                    x, y = (x1 + x2) // 2, (y1 + y2) // 2
                    canvas.event_generate('<Motion>', x=x, y=y)
                    canvas.event_generate('<Button-1>', x=x, y=y)
                    root.update()
                click('A01')  # Occupied: must not count as an assignment.
                click('P24')
                assert buttons['Use selected wells'].instate(['disabled'])
                click('B03')
                assert not buttons['Use selected wells'].instate(['disabled'])
                buttons['Undo last'].invoke()
                assert buttons['Use selected wells'].instate(['disabled'])
                click('C04')
                buttons['Use selected wells'].invoke()
        except BaseException as exc:
            errors.append(exc)
            window.destroy()

    root.after(100, interact)
    try:
        result = choose_destination_wells(root, runs, 'Plate', {'A01'}, 'A01')
        assert not errors, errors
        assert result == {'manual': ['P24', 'C04'], 'automatic': ['A02', 'A03'], 'cancel': None}[action]
    finally:
        root.destroy()
