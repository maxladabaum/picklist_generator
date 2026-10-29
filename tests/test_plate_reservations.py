from copy import deepcopy
import json

from app_state import clear_plate, occupied_destination_wells


def test_fresh_plate_releases_stored_reservations_without_deleting_runs():
    state = {'plates': {'Destination[1]': {'wells': {'A01': {}}},
                        'Other': {'wells': {'B01': {}}}}}
    items = [{'id': 'old', 'destination_plate_name': 'Destination[1]', 'destination_wells': ['E01', 'E16']},
             {'id': 'other', 'destination_plate_name': 'Other', 'destination_wells': ['E01']}]
    original = deepcopy(items)
    assert occupied_destination_wells(state, 'Destination[1]', items) == {'A01', 'E01', 'E16'}
    assert clear_plate(state, 'Destination[1]', items)
    state = json.loads(json.dumps(state))  # Persisted reset survives restart.
    assert occupied_destination_wells(state, 'Destination[1]', items) == set()
    assert occupied_destination_wells(state, 'Other', items) == {'B01', 'E01'}
    assert items == original
    items.append({'id': 'new', 'destination_plate_name': 'Destination[1]', 'destination_wells': ['E01']})
    assert occupied_destination_wells(state, 'Destination[1]', items) == {'E01'}
    assert occupied_destination_wells(state, 'Destination[1]', items, {'new'}) == set()
    state['plates']['Destination[1]'] = {'wells': {'A02': {}}}
    assert occupied_destination_wells(state, 'Destination[1]', items) == {'A02', 'E01'}
    clear_plate(state, 'Destination[1]', items)
    assert occupied_destination_wells(state, 'Destination[1]', items) == set()


def test_already_empty_plate_still_releases_storage_reservations():
    state = {'plates': {}}
    items = [{'id': 'old', 'destination_plate_name': 'Destination[1]', 'destination_wells': ['E01']}]
    assert clear_plate(state, 'Destination[1]', items)
    assert occupied_destination_wells(state, 'Destination[1]', items) == set()


def test_legacy_state_keeps_reservations_until_explicit_reset():
    state = {'plates': {}}
    original = deepcopy(state)
    items = [{'id': 'old', 'destination_plate_name': 'Destination[1]', 'destination_wells': ['E01']}]
    assert occupied_destination_wells(state, 'Destination[1]', items) == {'E01'}
    assert state == original
