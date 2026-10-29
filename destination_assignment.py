"""Destination assignment shared by the RGB plate picker and batch preparation."""
from copy import deepcopy

from app_state import all_384_wells


def assignment_slots(runs):
    return [(run['run_name'], index + 1, len(run['destination_wells']))
            for run in runs for index in range(len(run['destination_wells']))]


def assign_destination_wells(runs, wells, used=()):
    """Copy a prepared batch to explicit wells, preserving transfers and recipes."""
    wells = list(wells)
    if len(wells) != len(assignment_slots(runs)):
        raise ValueError('Select exactly one destination well for each required run well.')
    if len(set(wells)) != len(wells):
        raise ValueError('Each destination well may be assigned only once.')
    if set(wells) - set(all_384_wells()):
        raise ValueError('Choose valid destination wells from A01 through P24.')
    if set(wells) & set(used):
        raise ValueError('Some selected destination wells are already occupied.')
    result = deepcopy(runs)
    offset = 0
    for run in result:
        count = len(run['destination_wells'])
        selected = wells[offset:offset + count]
        mapping = dict(zip(run['destination_wells'], selected))
        for transfer in run['picklist']:
            transfer['Destination Well'] = mapping[transfer['Destination Well']]
        run['destination_wells'] = selected
        offset += count
    return result


def automatic_destination_wells(count, used, start_well):
    wells = all_384_wells()
    value = start_well.strip().upper()
    if len(value) >= 2 and value[1:].isdigit():
        value = value[0] + value[1:].zfill(2)
    if value not in wells:
        raise ValueError('Enter a starting well from A01 through P24.')
    available = [well for well in wells[wells.index(value):] if well not in set(used)]
    if len(available) < count:
        raise ValueError('Not enough unused wells remain after the starting well.')
    return available[:count]


def choose_destination_wells(parent, runs, plate, used, start_well):
    """Ask for automatic or ordered manual assignment without changing plate state."""
    import tkinter as tk
    from tkinter import ttk, messagebox

    slots = assignment_slots(runs)
    used = set(used)
    wells = all_384_wells()
    selected = []
    result = None
    window = tk.Toplevel(parent)
    window.title('Assign destination wells — {}'.format(plate))
    window.transient(parent.winfo_toplevel())
    window.protocol('WM_DELETE_WINDOW', window.destroy)
    frame = ttk.Frame(window, padding=12)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Assign {} wells for {} runs on {}'.format(len(slots), len(runs), plate),
              font=('TkDefaultFont', 13, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Choose automatic assignment or click destination wells manually.').pack(anchor='w', pady=6)
    choices = ttk.Frame(frame)
    choices.pack(fill='x')
    start = tk.StringVar(value=start_well)

    def finish(values):
        nonlocal result
        result = values
        window.destroy()

    def automatic():
        try:
            available = automatic_destination_wells(len(slots), used, start.get())
        except ValueError as exc:
            messagebox.showerror('Cannot assign automatically', str(exc), parent=window)
            return
        finish(available)

    ttk.Button(choices, text='Assign automatically', command=automatic).pack(side='left')
    ttk.Label(choices, text='Start at well').pack(side='left', padx=(12, 4))
    ttk.Entry(choices, textvariable=start, width=6).pack(side='left')
    manual = ttk.Frame(frame)
    ttk.Button(choices, text='Assign manually', command=lambda: manual.pack(fill='both', expand=True, pady=(12, 0))).pack(side='left', padx=12)
    ttk.Button(choices, text='Cancel', command=window.destroy).pack(side='right')
    status = tk.StringVar()
    ttk.Label(manual, textvariable=status).pack(anchor='w')
    ttk.Label(manual, text='Gray: occupied   Blue: selected (assignment order shown). Click a selected well to remove it.').pack(anchor='w', pady=4)
    canvas = tk.Canvas(manual, width=920, height=500, background='white', highlightthickness=0)
    canvas.pack(fill='both', expand=True)
    controls = ttk.Frame(manual)
    controls.pack(fill='x', pady=6)
    confirm = ttk.Button(controls, text='Use selected wells', command=lambda: finish(list(selected)))
    confirm.pack(side='right')

    detail = tk.StringVar(value='Hover over a selected well to review its run.')
    ttk.Label(manual, textvariable=detail).pack(anchor='w')

    def show_assignment(well):
        if well in selected:
            name, part, total = slots[selected.index(well)]
            detail.set('{}: {} (well {} of {})'.format(well, name, part, total))
        else:
            detail.set('{}: {}'.format(well, 'occupied' if well in used else 'available'))

    def redraw():
        canvas.delete('all')
        for column in range(24):
            canvas.create_text(52 + column * 37, 14, text=str(column + 1))
        order = {well: index + 1 for index, well in enumerate(selected)}
        for row in range(16):
            canvas.create_text(14, 43 + row * 29, text=chr(65 + row))
            for column in range(24):
                well = '{}{:02d}'.format(chr(65 + row), column + 1)
                x, y = 35 + column * 37, 30 + row * 29
                fill = '#d1d5db' if well in used else '#2563eb' if well in order else '#f3f4f6'
                canvas.create_rectangle(x, y, x + 34, y + 26, fill=fill, outline='#6b7280', tags=(well,))
                canvas.create_text(x + 17, y + 13, text=str(order[well]) if well in order else '×' if well in used else '',
                                   fill='white' if well in order else '#555555', tags=(well,))
                canvas.tag_bind(well, '<Button-1>', lambda event, w=well: toggle(w))
                canvas.tag_bind(well, '<Enter>', lambda event, w=well: show_assignment(w))
        count = len(selected)
        if count < len(slots):
            name, part, total = slots[count]
            status.set('{} / {} wells selected. Next: {} (well {} of {}).'.format(count, len(slots), name, part, total))
        else:
            status.set('{} / {} wells selected. Ready to prepare.'.format(count, len(slots)))
        confirm.configure(state='normal' if count == len(slots) else 'disabled')

    def toggle(well):
        if well in used:
            return
        if well in selected:
            selected.remove(well)
        elif len(selected) < len(slots):
            selected.append(well)
        redraw()

    def undo():
        if selected:
            selected.pop()
        redraw()

    def clear():
        selected.clear()
        redraw()

    ttk.Button(controls, text='Undo last', command=undo).pack(side='left')
    ttk.Button(controls, text='Clear selection', command=clear).pack(side='left', padx=6)
    redraw()
    window.bind('<Escape>', lambda event: window.destroy())
    window.wait_visibility()
    window.grab_set()
    parent.wait_window(window)
    return result
