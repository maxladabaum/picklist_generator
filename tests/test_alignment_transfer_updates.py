"""Cached alignment geometry must not retain an earlier transfer grid."""
import numpy as np
import pytest

from alignment_reconstruction import AlignmentModel, AlignmentSettings
from app_state import all_384_wells
from image_encoding import CHANNELS, compile_designs, prepare_runs
from picklist_core import parse_source2
from test_image_encoding import SHEETS, SETTINGS, MIXING


def assignments(volume, stock):
    designs = compile_designs(
        [[[210, 170, 90] for _ in range(6)] for _ in range(8)],
        volume, {c: 1 for c in CHANNELS}, 'tiles_1x1',
        plate_defaults_nm={c: 25 for c in CHANNELS},
        base_concentration_nm=stock,
        source_concentrations_nm={c: 20000 for c in CHANNELS},
        plate_relative_transfers=True)
    # Keep the regression fast while preserving a valid selection for export.
    for design in designs:
        design['image_sites'] = design['image_sites'][:2]
        keep = {s['name'] for s in design['image_sites']}
        design['site_fractions'] = {k: v for k, v in design['site_fractions'].items() if k in keep}
    return designs


@pytest.mark.parametrize('volume,stock', [(200, 20000), (200, 200000)])
def test_reused_alignment_model_uses_current_transfer_grid(volume, stock):
    old = assignments(150, 20000)
    current = assignments(volume, stock)
    settings = AlignmentSettings(0, 0, 0, 3)
    cached = AlignmentModel(old, settings)
    fresh = AlignmentModel(current, settings)
    target = np.clip(cached.expected(old).density * cached.gain, 0, 1)
    optimized, _ = cached.optimize(current, target)
    expected, _ = fresh.optimize(current, target)
    assert optimized == expected

    sources = {c: (SHEETS / 'PAINT_R1_replace.csv', 'SourcePlate7[7]') for c in CHANNELS}
    base = 'book_base_20uM.csv' if stock == 20000 else 'book_base.csv'
    runs = prepare_runs(optimized, SHEETS / base, sources, all_384_wells(), SETTINGS, MIXING)
    rows, _ = parse_source2(SHEETS / 'PAINT_R1_replace.csv')
    wells = {row['Name']: row['Well'].upper() for row in rows}
    for design, run in zip(optimized, runs):
        actual = {row['Source Well']: row['Transfer Volume'] for row in run['picklist']
                  if row['Source Plate Name'] == 'SourcePlate7[7]'}
        for site in design['image_sites']:
            fraction = site['replacement_fraction']
            assert site['replacement_nl'] == pytest.approx(design['full_replacement_nl'] * fraction)
            assert site['original_nl'] == pytest.approx(design['full_original_nl'] * (1 - fraction))
            assert actual.get(wells[site['name']], 0) == site['replacement_nl']
