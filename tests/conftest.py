"""Keep existing workflow tests noninteractive at the destination-choice prompt."""
import sys

import pytest

import destination_assignment


@pytest.fixture(autouse=True)
def automatic_destination_choice(monkeypatch, request):
    # Picker-specific tests can exercise the actual dialog; existing workflow
    # tests retain their automatic assignment behavior without waiting for clicks.
    if request.module.__name__ == 'test_destination_assignment':
        return

    def choose(parent, runs, plate, used, start_well):
        return destination_assignment.automatic_destination_wells(
            len(destination_assignment.assignment_slots(runs)), used, start_well)

    monkeypatch.setattr(destination_assignment, 'choose_destination_wells', choose)
    view = sys.modules.get('rgb_image_view')
    if view is not None:
        monkeypatch.setattr(view, 'choose_destination_wells', choose)
